"""The extended-key vectors for spec 031, every one in test-network form, and their provenance.

**Rule 3 and spec 031's R11 forbid a mainnet extended key or a mainnet address anywhere in
the repository, as a literal or assembled at run time.** The published vectors for BIP-32 and
BIP-84 are mainnet, so they appear here only after a conversion that never wrote the mainnet
form to any file:

* **BIP-32** (`bitcoin/bips`, `bip-0032.mediawiki`, "Test Vectors"). The text was read into
  memory by a throwaway script. That script carried its own secp256k1, HMAC-SHA512 child
  derivation and Base58Check, sharing nothing with `portfolio.domain`, and it first
  re-derived **every** published extended public and private key of test vectors 1 to 4 from
  the published seeds, comparing each with the published string in memory. Only then did it
  re-serialise each published public key with the `tpub` version bytes `0x043587CF` in place
  of `0x0488B21E`, recomputing the checksum. Depth, parent fingerprint, child number, chain
  code and key are the published bytes unchanged. It also checked every non-hardened
  parent-to-child step below by public derivation alone.
* **BIP-49** (`bip-0049.mediawiki`, "Test vectors"). Published in test-network form already:
  the `upub` account key, the first receive public key, its key hash, and the P2SH-P2WPKH
  address are quoted verbatim. The independent script re-derived the public key from the
  `upub` by public derivation and re-derived the address from it.
* **BIP-84** (`bip-0084.mediawiki`, "Test vectors"). The root and account `zpub` keys were
  re-serialised with the `vpub` version bytes `0x045F1CF6`. Each published address was
  decoded in memory to its witness program, checked equal to HASH160 of the published public
  key, and re-encoded under `tb` (and `bcrt`) over the **same** program. The public keys are
  quoted verbatim, since a public key is not an address.
* **Derived** values are marked as such. They come from the same independent script, after it
  had reproduced every published value above.

The private keys that the BIP texts also publish appear nowhere here, in any form. A
private-key refusal is tested with a short prefixed string that is not key-shaped.
"""

from __future__ import annotations

from typing import Final, NamedTuple

# --------------------------------------------------------------------------------------
# BIP-32, test vectors 1 to 4, public keys re-versioned to `tpub`
# --------------------------------------------------------------------------------------

#: Test vector 1, chain m. Depth 0, null fingerprint, child number 0.
BIP32_TV1_M: Final = (
    "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1RhGjxbaTLV3X4"
    "FyWuejifB9jusQ46QzG87VKp"
)
#: Test vector 1, chain m/0H.
BIP32_TV1_M_0H: Final = (
    "tpubD8eQVK4Kdxg3gHrF62jGP7dKVCoYiEB8dFSpuTawkL5YxTus5j5pf83vaKnii4bc6v2NVEy81P2gYrJczYn"
    "e3QNNwMTS53p5uzDyHvnw2jm"
)
#: Test vector 1, chain m/0H/1: a non-hardened public step from m/0H.
BIP32_TV1_M_0H_1: Final = (
    "tpubDApXh6cD2fZ7WjtgpHd8yrWyYaneiFuRZa7fVjMkgxsmC1QzoXW8cgx9zQFJ81Jx4deRGfRE7yXA9A3STsx"
    "Xj4CKEZJHYgpMYikkas9DBTP"
)
#: Test vector 1, chain m/0H/1/2H.
BIP32_TV1_M_0H_1_2H: Final = (
    "tpubDDRojdS4jYQXNugn4t2WLrZ7mjfAyoVQu7MLk4eurqFCbrc7cHLZX8W5YRS8ZskGR9k9t3PqVv68bVBjAyW"
    "4nWM9pTGRddt3GQftg6MVQsm"
)
#: Test vector 1, chain m/0H/1/2H/2.
BIP32_TV1_M_0H_1_2H_2: Final = (
    "tpubDFfCa4Z1v25WTPAVm9EbEMiRrYwucPocLbEe12BPBGooxxEUg42vihy1DkRWyftztTsL23snYezF9uXjGGw"
    "GW6pQjEpcTpmsH6ajpf4CVPn"
)
#: Test vector 1, chain m/0H/1/2H/2/1000000000.
BIP32_TV1_M_0H_1_2H_2_1000000000: Final = (
    "tpubDHNy3kAG39ThyiwwsgoKY4iRenXDRtce8qdCFJZXPMCJg5dsCUHayp84raLTpvyiNA9sXPob5rgqkKvkN8S"
    "7MMyXbnEhGJMW64Cf4vFAoaF"
)

#: Test vector 2, chain m.
BIP32_TV2_M: Final = (
    "tpubD6NzVbkrYhZ4XJDrzRvuxHEyQaPd1mwwdDofEJwekX18tAdsqeKfxss79AJzg1431FybXg5rfpTrJF4iAhy"
    "R7RubberdzEQXiRmXGADH2eA"
)
#: Test vector 2, chain m/0.
BIP32_TV2_M_0: Final = (
    "tpubD9ejmKSp2iP93ZpA8DJo25eVmY8sikSEBPZ2Q7y6pvs6a95rQufk7iSMidGtU64UDaTmPu5c4uJpTQVQ3rf"
    "qT2ZsshbJtaYuqutBhMEvKgw"
)
#: Test vector 2, chain m/0/2147483647H.
BIP32_TV2_M_0_2147483647H: Final = (
    "tpubDAoo1vULQcZFDS2LYfJSVRL4AHMnGEbvGYdZKWssUfdV2SKK2o64KnDxL1X1Dpfa16PK3jwDN7jR85Mjpm9"
    "xBB2WQnDNFJoviJ9nYAGqm3T"
)
#: Test vector 2, chain m/0/2147483647H/1.
BIP32_TV2_M_0_2147483647H_1: Final = (
    "tpubDDcmRwTGaFrSK3hUcKT1TNGHVpEHNRXBaz8RAaYCnYyvhGBULJcmDgcLAoi91hMrbGqrtP2T1F3FCsckjfa"
    "uSWVR14RDTrF8e4pjnhENZ4d"
)
#: Test vector 2, chain m/0/2147483647H/1/2147483646H.
BIP32_TV2_M_0_2147483647H_1_2147483646H: Final = (
    "tpubDEnoLuPdBep9bzw5LoGYpsxUQYheRQ9gcgrJhJEcdKFB9cWQRyYmkCyRoTqeD4tJYiVVgt6A3rN6rWn9RYh"
    "R9sBsGxji29LYWHuKKbdb1ev"
)
#: Test vector 2, chain m/0/2147483647H/1/2147483646H/2.
BIP32_TV2_M_0_2147483647H_1_2147483646H_2: Final = (
    "tpubDG9qJLc8hq8PMG7y4sQEodLSocEkfj4mGrUC75b7G76mDoqybcUXvmvRsruvLeF14mhixobZwZP6LwqeFeP"
    "KU83Sv8ZnxWdHBb6VzE6zbvC"
)

#: Test vector 3, chain m: the retention of leading zeros.
BIP32_TV3_M: Final = (
    "tpubD6NzVbkrYhZ4WMg2WpRiAGW1HDiime7DLZaDdbyD2D5vrQs9VnZdv96dd9qyVbdgBLjTMfxs4VHhjYhd1R1"
    "rXmZjitkrinrNW9HDndbLQPW"
)
#: Test vector 3, chain m/0H.
BIP32_TV3_M_0H: Final = (
    "tpubD8kCEZazE4vQhtmRjxmDDXFfyaL6vVX7k3pASqf3xX1J7Rzc5HLVzbtLvsgVDxERNiEJ8dibuSVCN1dxwex"
    "371qgPzhkGeMAzKe8T7ivSof"
)
#: Test vector 4, chain m.
BIP32_TV4_M: Final = (
    "tpubD6NzVbkrYhZ4YRBbMYnWy1xtuTJtgtWxahGd1LentjvchJgUhhAQ8MejKH6eX3Djkss1WMC41G4SYRUkRd2"
    "mcdReVK3wH6pBQjxSsTSqStL"
)
#: Test vector 4, chain m/0H.
BIP32_TV4_M_0H: Final = (
    "tpubD9Y6sysWvTfWBJCiysiHyR4fMPCAQC3WKztHMJpmYBzbBqjbG2b8GQL5UsNQYfasAydNTPafcBzh5MAp8QV"
    "AhwUgXdUu9uuZTYWVfsxJxZr"
)
#: Test vector 4, chain m/0H/1H.
BIP32_TV4_M_0H_1H: Final = (
    "tpubDBfnXyGXSBi6rT4N89dC5HeWXKNEu7kJyvw784LyCP62xw478X4qVXHxNp8wPrDYkmEjMAWn4eLg3qVib1J"
    "KRER3sRKx7ePWKxsPjSjMcih"
)


class ChildStep(NamedTuple):
    """A published non-hardened step: the parent key, the index, and the published child."""

    id: str
    parent: str
    child_index: int
    child: str


#: Every non-hardened public step in test vectors 1 and 2. A hardened step needs the
#: private key and is not something this application can do (spec 031, non-goals).
BIP32_PUBLIC_STEPS: Final[tuple[ChildStep, ...]] = (
    ChildStep("tv1 m/0H -> /1", BIP32_TV1_M_0H, 1, BIP32_TV1_M_0H_1),
    ChildStep("tv1 m/0H/1/2H -> /2", BIP32_TV1_M_0H_1_2H, 2, BIP32_TV1_M_0H_1_2H_2),
    ChildStep(
        "tv1 m/0H/1/2H/2 -> /1000000000",
        BIP32_TV1_M_0H_1_2H_2,
        1_000_000_000,
        BIP32_TV1_M_0H_1_2H_2_1000000000,
    ),
    ChildStep("tv2 m -> /0", BIP32_TV2_M, 0, BIP32_TV2_M_0),
    ChildStep(
        "tv2 m/0/2147483647H -> /1", BIP32_TV2_M_0_2147483647H, 1, BIP32_TV2_M_0_2147483647H_1
    ),
    ChildStep(
        "tv2 m/0/2147483647H/1/2147483646H -> /2",
        BIP32_TV2_M_0_2147483647H_1_2147483646H,
        2,
        BIP32_TV2_M_0_2147483647H_1_2147483646H_2,
    ),
)


class ParsedShape(NamedTuple):
    """A key with the structural fields its published path fixes."""

    id: str
    key: str
    depth: int
    child_number: int


#: Every BIP-32 public key above with the depth and child number its path states. The
#: hardened ones are here too: parsing a hardened child's public key is ordinary.
BIP32_ALL: Final[tuple[ParsedShape, ...]] = (
    ParsedShape("tv1 m", BIP32_TV1_M, 0, 0),
    ParsedShape("tv1 m/0H", BIP32_TV1_M_0H, 1, 0x80000000),
    ParsedShape("tv1 m/0H/1", BIP32_TV1_M_0H_1, 2, 1),
    ParsedShape("tv1 m/0H/1/2H", BIP32_TV1_M_0H_1_2H, 3, 0x80000002),
    ParsedShape("tv1 m/0H/1/2H/2", BIP32_TV1_M_0H_1_2H_2, 4, 2),
    ParsedShape("tv1 m/0H/1/2H/2/1000000000", BIP32_TV1_M_0H_1_2H_2_1000000000, 5, 1_000_000_000),
    ParsedShape("tv2 m", BIP32_TV2_M, 0, 0),
    ParsedShape("tv2 m/0", BIP32_TV2_M_0, 1, 0),
    ParsedShape("tv2 m/0/2147483647H", BIP32_TV2_M_0_2147483647H, 2, 0xFFFFFFFF),
    ParsedShape("tv2 m/0/2147483647H/1", BIP32_TV2_M_0_2147483647H_1, 3, 1),
    ParsedShape(
        "tv2 m/0/2147483647H/1/2147483646H",
        BIP32_TV2_M_0_2147483647H_1_2147483646H,
        4,
        0xFFFFFFFE,
    ),
    ParsedShape(
        "tv2 m/0/2147483647H/1/2147483646H/2", BIP32_TV2_M_0_2147483647H_1_2147483646H_2, 5, 2
    ),
    ParsedShape("tv3 m", BIP32_TV3_M, 0, 0),
    ParsedShape("tv3 m/0H", BIP32_TV3_M_0H, 1, 0x80000000),
    ParsedShape("tv4 m", BIP32_TV4_M, 0, 0),
    ParsedShape("tv4 m/0H", BIP32_TV4_M_0H, 1, 0x80000000),
    ParsedShape("tv4 m/0H/1H", BIP32_TV4_M_0H_1H, 2, 0x80000001),
)

# --------------------------------------------------------------------------------------
# BIP-32, test vector 5: the public invalid keys, re-versioned to `tpub`
# --------------------------------------------------------------------------------------
#
# Re-versioned exactly as above: the 74 bytes after the version are the published ones, so
# each still carries the defect its label names, and the checksum is recomputed so that the
# refusal has to come from that defect rather than from the checksum.

#: "pubkey version / prvkey mismatch": a public version over private key data, which starts
#: with a `00` byte. As a public key that is a prefix other than 02 or 03.
TV5_PUBKEY_VERSION_PRVKEY_DATA: Final = (
    "tpubD6NzVbkrYhZ4WLczPJWReQycCJdd6YVWXubbVUFnJ5KgU5MDQrD998ZJLNGbhd2pq7ZtDiPYTfJ7iBenLVQ"
    "pYgSQqPjUsQeJXH8VRWiCrQf"
)
#: "invalid pubkey prefix 04": an uncompressed-key prefix where 02 or 03 is required.
TV5_PUBKEY_PREFIX_04: Final = (
    "tpubD6NzVbkrYhZ4WLczPJWReQycCJdd6YVWXubbVUFnJ5KgU5MDQrD998ZJLW3aQYpxEpGnkziN5yn7XU3szws"
    "h9Vouyx1ur1PtVuA3Pv7LzUg"
)
#: "invalid pubkey prefix 01".
TV5_PUBKEY_PREFIX_01: Final = (
    "tpubD6NzVbkrYhZ4WLczPJWReQycCJdd6YVWXubbVUFnJ5KgU5MDQrD998ZJLQDLsrUrgYF7MXyFNEv7fWFZFc2"
    "YCPHHd2ob7ZLCWw8sv7REuNa"
)
#: "invalid pubkey 020000...0007": a well-formed prefix over an x with no point on the curve.
TV5_PUBKEY_NOT_ON_CURVE: Final = (
    "tpubD6NzVbkrYhZ4WLczPJWReQycCJdd6YVWXubbVUFnJ5KgU5MDQrD998ZJLSA645vtXxvLVMYxGpY7cprLAie"
    "Fr68AQfshMi26Wb9GQBcNw9z"
)
#: "zero depth with non-zero parent fingerprint" (fingerprint 01010101).
TV5_DEPTH_ZERO_WITH_FINGERPRINT: Final = (
    "tpubD6PRKLEwwo1MaYiv3ZmUprPqkjW7qqDf2BegBrJ5RYwefQUdYAXobagMm4CH7jUW2HET2nAzF54eD2ueeWn"
    "QM4eXiCUQgKbbFNM7BayAsmG"
)
#: "zero depth with non-zero index" (child number 0x01010101).
TV5_DEPTH_ZERO_WITH_INDEX: Final = (
    "tpubD6NzVbkrcVaDMzcFXZ3vvCfDdAFBdPv4vcG7FuNboRNafEAKk5QPipbY4ggmo9ovubSWoWW6wcFxNjiWcPa"
    "c6sDq43Jwk8n1NQuYsq3Dd5r"
)

# --------------------------------------------------------------------------------------
# Derived refusals over test vector 1's master payload
# --------------------------------------------------------------------------------------
#
# Derived by the independent script: test vector 1's master public key, unchanged after the
# version, re-serialised under a version this application refuses, with a correct checksum.

#: Version `0x024289EF` (SLIP-0132 `Upub`, test-network P2SH-P2WSH multisig).
DERIVED_UPUB_MULTISIG: Final = (
    "Upub5JQfBberxLXY8a7hsfyeVyF939kTjFu9xwWe9tjZfyq9Jn5CbhbEcTvCJ7DwzxzyKLvTdb8GEsTQMacwno7"
    "vqrXDuZw17tGAteaweU9nndT"
)
#: Version `0x02575483` (SLIP-0132 `Vpub`, test-network P2WSH multisig).
DERIVED_VPUB_MULTISIG: Final = (
    "Vpub5dEvVGKn7251ysJpi2mGi4LeD7tufstet42rwHdT3zD2MstRrMkoEXaLKKBXzsetiz3GP4iphXoxEsEWWVX"
    "we6CpmudRho5fANeb32XJ5a2"
)
#: Version `0x043587D0`, one above `tpub`'s. It still renders with a `tpub` prefix, so it
#: reaches the decoder and has to be refused there, on its version.
DERIVED_UNKNOWN_VERSION_TPUB: Final = (
    "tpubMQ5gEWGCrhgSCffrftzDYMEbBNve62AB9ih7RVqWe59gYsYJkM45Afzu7v99UtFzXAjwEVKoeVCqonHMKAZ"
    "Z3u6MGgCBAur44uwQJcYfuhn"
)

# --------------------------------------------------------------------------------------
# BIP-49: published in test-network form
# --------------------------------------------------------------------------------------

#: `account0Xpub`, m/49'/1'/0'. Depth 3, child number 0x80000000.
BIP49_ACCOUNT_UPUB: Final = (
    "upub5EFU65HtV5TeiSHmZZm7FUffBGy8UKeqp7vw43jYbvZPpoVsgU93oac7Wk3u6moKegAEWtGNF8DehrnHtv2"
    "1XXEMYRUocHqguyjknFHYfgY"
)
#: `account0recvPublicKeyHex`, m/49'/1'/0'/0/0.
BIP49_RECEIVE_0_PUBLIC_KEY: Final = (
    "03a1af804ac108a8a51782198c2d034b28bf90c8803f5a53f76276fa69a4eae77f"
)
#: `keyhash = HASH160(account0recvPublicKeyHex)`.
BIP49_RECEIVE_0_KEY_HASH: Final = "38971f73930f6c141d977ac4fd4a727c854935b3"
#: `addressBytes = HASH160(scriptSig)`, the P2SH script hash.
BIP49_RECEIVE_0_SCRIPT_HASH: Final = "336caa13e08b96080a32b5d818d59b4ab3b36742"
#: `address`, the testnet P2SH-P2WPKH address.
BIP49_RECEIVE_0_ADDRESS: Final = "2Mww8dCYPUpKHofjgcXcBCEGmniw9CoaiD2"

#: Derived: the same key hash as a test-network P2PKH address (version 0x6F).
DERIVED_BIP49_RECEIVE_0_P2PKH: Final = "mkgBAzmFSVxiR7kAWRuYw6dNBbG69dgEbL"
#: Derived: the same key hash as a test-network P2WPKH address.
DERIVED_BIP49_RECEIVE_0_P2WPKH_TB: Final = "tb1q8zt37uunpakpg8vh0tz06jnj0jz5jddn5mlts3"
#: Derived: the same key hash as a regtest P2WPKH address.
DERIVED_BIP49_RECEIVE_0_P2WPKH_BCRT: Final = "bcrt1q8zt37uunpakpg8vh0tz06jnj0jz5jddnkjxx8c"

# --------------------------------------------------------------------------------------
# BIP-84, re-versioned to `vpub`, addresses as `tb1` over the same witness programs
# --------------------------------------------------------------------------------------

#: `rootpub`, re-versioned to `vpub`. Depth 0.
BIP84_ROOT_VPUB: Final = (
    "vpub5SLqN2bLY4WeZA14EtnYS6Byt1JD1QBXBCYsqz47UENzCqveqEk2bTLvgmEUCc2seD2SQzzqm1DKv2gTzK1"
    "Qj4R4XucjdmCNKNDtSgckK7x"
)
#: The account key m/84'/0'/0', re-versioned to `vpub`. Depth 3, child number 0x80000000.
BIP84_ACCOUNT_VPUB: Final = (
    "vpub5YvMuJNjRSYon44z9QmCfdf8SqJRVNvz6m55Qy5iVjZQxDfUgtiQjnc7CC1fAbED2tAGCZRERUfvtn2Dst"
    "ZGU6HMns6dXXH2wujSc2wfi2x"
)


class DerivedAddress(NamedTuple):
    """A published child of an account key: where it is, its public key, and its address."""

    id: str
    branch: int
    child_index: int
    public_key: str
    address: str


#: BIP-84's three published children, the public keys verbatim and each address as `tb1`.
BIP84_CHILDREN: Final[tuple[DerivedAddress, ...]] = (
    DerivedAddress(
        "bip84 /0/0",
        0,
        0,
        "0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c",
        "tb1qcr8te4kr609gcawutmrza0j4xv80jy8zmfp6l0",
    ),
    DerivedAddress(
        "bip84 /0/1",
        0,
        1,
        "03e775fd51f0dfb8cd865d9ff1cca2a158cf651fe997fdc9fee9c1d3b5e995ea77",
        "tb1qnjg0jd8228aq7egyzacy8cys3knf9xvrn9d67m",
    ),
    DerivedAddress(
        "bip84 /1/0",
        1,
        0,
        "03025324888e429ab8e3dbaf1f7802648b9cd01e9b418485c5fa4c1b9b5700e1a6",
        "tb1q8c6fshw2dlwun7ekn9qwf37cu2rn755ut76fzv",
    ),
)

#: BIP-49's published child, for the same table shape.
BIP49_CHILDREN: Final[tuple[DerivedAddress, ...]] = (
    DerivedAddress("bip49 /0/0", 0, 0, BIP49_RECEIVE_0_PUBLIC_KEY, BIP49_RECEIVE_0_ADDRESS),
)


class Encodings(NamedTuple):
    """One public key under every script type, on testnet and on regtest (R3)."""

    id: str
    public_key: str
    testnet_p2pkh: str
    testnet_p2sh_p2wpkh: str
    testnet_p2wpkh: str
    regtest_p2pkh: str
    regtest_p2sh_p2wpkh: str
    regtest_p2wpkh: str


#: Derived, from BIP-84's published public keys and BIP-49's. On regtest the base58 version
#: bytes are testnet's (0x6F, 0xC4), so those two columns repeat; only the bech32 part
#: changes, to `bcrt`. That repetition is R3's table, and it is asserted rather than assumed.
ENCODINGS: Final[tuple[Encodings, ...]] = (
    Encodings(
        "bip84 /0/0",
        "0330d54fd0dd420a6e5f8d3624f5f3482cae350f79d5f0753bf5beef9c2d91af3c",
        "my6RhGaMEf8v9yyQKqiuUYniJLfyU4gzqe",
        "2N8ShdHvtvhbbrWPBQkgTqvNtP5Bp33veEi",
        "tb1qcr8te4kr609gcawutmrza0j4xv80jy8zmfp6l0",
        "my6RhGaMEf8v9yyQKqiuUYniJLfyU4gzqe",
        "2N8ShdHvtvhbbrWPBQkgTqvNtP5Bp33veEi",
        "bcrt1qcr8te4kr609gcawutmrza0j4xv80jy8zeqchgx",
    ),
    Encodings(
        "bip84 /0/1",
        "03e775fd51f0dfb8cd865d9ff1cca2a158cf651fe997fdc9fee9c1d3b5e995ea77",
        "munoNuscNJfEbrQyEQt1CmYDeNtQseT378",
        "2N6erLsHUv6mpaiHS6UVy3EEtNU1mtgF6Bq",
        "tb1qnjg0jd8228aq7egyzacy8cys3knf9xvrn9d67m",
        "munoNuscNJfEbrQyEQt1CmYDeNtQseT378",
        "2N6erLsHUv6mpaiHS6UVy3EEtNU1mtgF6Bq",
        "bcrt1qnjg0jd8228aq7egyzacy8cys3knf9xvr3v5hfj",
    ),
    Encodings(
        "bip84 /1/0",
        "03025324888e429ab8e3dbaf1f7802648b9cd01e9b418485c5fa4c1b9b5700e1a6",
        "mmBsCKnjnyGQbHanuXgRRocN43Tmb1TLJG",
        "2N6HZAqLDHQGHhb1sFRYkdZMFEijiXD7Yvx",
        "tb1q8c6fshw2dlwun7ekn9qwf37cu2rn755ut76fzv",
        "mmBsCKnjnyGQbHanuXgRRocN43Tmb1TLJG",
        "2N6HZAqLDHQGHhb1sFRYkdZMFEijiXD7Yvx",
        "bcrt1q8c6fshw2dlwun7ekn9qwf37cu2rn755ufhry49",
    ),
    Encodings(
        "bip49 /0/0",
        BIP49_RECEIVE_0_PUBLIC_KEY,
        DERIVED_BIP49_RECEIVE_0_P2PKH,
        BIP49_RECEIVE_0_ADDRESS,
        DERIVED_BIP49_RECEIVE_0_P2WPKH_TB,
        DERIVED_BIP49_RECEIVE_0_P2PKH,
        BIP49_RECEIVE_0_ADDRESS,
        DERIVED_BIP49_RECEIVE_0_P2WPKH_BCRT,
    ),
)

#: Derived: BIP-32 test vector 1's master `tpub`, children /0/0 and /1/0, as test-network
#: P2PKH, which is what a `tpub` derives (R2).
TV1_MASTER_CHILDREN_P2PKH: Final[tuple[DerivedAddress, ...]] = (
    DerivedAddress(
        "tv1 m/0/0",
        0,
        0,
        "02756de182c5dd4b717ea87e693006da62dbb3cddaa4a5cad2ed1f5bbab755f0f5",
        "mgiHMN7dJsANUWwLfgbiw7hc4kR5xMjPhw",
    ),
    DerivedAddress(
        "tv1 m/1/0",
        1,
        0,
        "029b393153a1ec68c7af3a98e88aecede3a409f27e698c090540098611c79e05b0",
        "n3TCBJe5GeYsbS3vc7n2gteRSuyjTqqFer",
    ),
)

# --------------------------------------------------------------------------------------
# The gap-scan fixture (criterion 3): BIP-84's account key, as `vpub`
# --------------------------------------------------------------------------------------
#
# Derived by the independent script from `BIP84_ACCOUNT_VPUB`, after it had reproduced the
# three published children above. Receive is branch 0, change is branch 1.

SCAN_KEY: Final = BIP84_ACCOUNT_VPUB

#: Receive branch, by index.
SCAN_RECEIVE: Final[dict[int, str]] = {
    0: "tb1qcr8te4kr609gcawutmrza0j4xv80jy8zmfp6l0",
    1: "tb1qnjg0jd8228aq7egyzacy8cys3knf9xvrn9d67m",
    5: "tb1qnpzzqjzet8gd5gl8l6gzhuc4s9xv0djt99y09w",
    23: "tb1qut5hjrs2l8lxk5rrmt9a0s0z9237h44sgs56ls",
    24: "tb1qrmxgay29sg36z4fjkgzqfjtar0xuztpnumstdy",
    44: "tb1qyxzd5whpsnlu3m64uhus6yf6e7sy3gx428u36v",
    45: "tb1qjdgksgwe7r69rgzrk2ewe2qw602vurcvf2qmap",
    46: "tb1qmdy8zg0rt6x6x9vw5l466wqww27w30cmc28xsy",
    64: "tb1qhyvcl8r5enceecm8adgfufn6duv3qdml7d7h8v",
    65: "tb1qad5458hx3wnlhhxfn76rhkaftrghrvl6l928al",
}

#: Change branch, by index.
SCAN_CHANGE: Final[dict[int, str]] = {
    0: "tb1q8c6fshw2dlwun7ekn9qwf37cu2rn755ut76fzv",
    3: "tb1qv6vaedpeke2lxr3q0wek8dd7nzhut9w0nxd373",
    22: "tb1qnc2w0jsp4d047j6rynzxdk608cvphf9vz0n0u3",
    23: "tb1q0m2fsjmmvlwegclljlttmdun9376m8pqc8epag",
    24: "tb1q7c9vvlnw68u6vqarv37h6cvzavs4mc2jekgc3t",
}

#: The used addresses criterion 3 names. Receive 46 is more than twenty past 24 and must
#: never be asked about: that is the gap limit working, not a bug.
SCAN_USED_RECEIVE: Final = (0, 5, 24, 46)
SCAN_USED_CHANGE: Final = (0, 3)

# --------------------------------------------------------------------------------------
# Prefix refusals: short, and not key-shaped
# --------------------------------------------------------------------------------------

#: Every private prefix R2 names. A refusal by prefix needs nothing after the prefix, so the
#: test strings are the prefix plus a few characters: never the shape of a real key.
PRIVATE_PREFIXES: Final = (
    "xprv",
    "yprv",
    "zprv",
    "tprv",
    "uprv",
    "vprv",
    "Yprv",
    "Zprv",
    "Uprv",
    "Vprv",
)

#: The multisig public prefixes R2 refuses by prefix.
MULTISIG_PREFIXES: Final = ("Ypub", "Zpub", "Upub", "Vpub")

#: The six single-signature public prefixes, mainnet ones included, as prefixes only.
SINGLE_SIG_PREFIXES: Final = ("xpub", "ypub", "zpub", "tpub", "upub", "vpub")

#: A short tail. With it, a prefixed string is eight characters: obviously not a key.
SHORT_TAIL: Final = "8Zgx"


def short(prefix: str) -> str:
    """A prefixed string too short to be a key, for the refusals that are by prefix."""
    return f"{prefix}{SHORT_TAIL}"


#: The version table of R2, as integers. Mainnet support is proven against these (R11).
R2_VERSIONS: Final[dict[str, tuple[int, str, str]]] = {
    "xpub": (0x0488B21E, "main", "p2pkh"),
    "ypub": (0x049D7CB2, "main", "p2sh-p2wpkh"),
    "zpub": (0x04B24746, "main", "p2wpkh"),
    "tpub": (0x043587CF, "test", "p2pkh"),
    "upub": (0x044A5262, "test", "p2sh-p2wpkh"),
    "vpub": (0x045F1CF6, "test", "p2wpkh"),
}
