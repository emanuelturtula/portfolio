# 031 — Bitcoin extended public keys, derived locally

Issue: #24
Status: done

## Problem

A Bitcoin wallet that hands out a fresh address for every receive and every change output
cannot be tracked one pasted address at a time. The owner would have to register each new
address by hand, and a balance that moved to a change address would simply disappear from
the total. Registration refuses an extended public key today (`AddressRejection.EXTENDED_KEY`).

Neither public Esplora instance this application reads (mempool.space, blockstream.info)
serves a lookup by extended key: their REST APIs are per address and per script hash. The
derivation therefore has to happen here.

## Scope

- Registration accepts a Bitcoin single-signature extended public key in the existing
  `address` field: `xpub`, `ypub`, `zpub`, and their test-network forms `tpub`, `upub`,
  `vpub`. It is parsed and validated offline.
- Local BIP32 public derivation of the receive (`/0/i`) and change (`/1/i`) branches,
  relative to the key as given. The script type comes from the prefix (R2).
- A gap-limit-20 scan through the per-address endpoint already in use, through the shared
  client and therefore the host rate limiter.
- Derived addresses are persisted, so a rescan derives only what it has not derived before.
- The wallet's balance snapshot is the sum of its derived addresses' balances. Everything
  downstream (dashboard, reconciliation, health) keeps reading one snapshot per wallet.
- The key is stored in the database only. The API serves a masked form, logs redact it by
  value, and no error message names it.
- A private extended key is refused at registration, both client side (never sent) and
  server side, and is redacted from logs.
- The frontend accepts an extended key in the wallet form and shows its masked form.
- `docs/providers.md` records why the public APIs cannot do this lookup.

## Non-goals

- **Taproot (BIP86), multisig (`Ypub`/`Zpub`/`Upub`/`Vpub`), output descriptors and
  a script-type override.** A key exported as `xpub` for a segwit account derives P2PKH
  addresses and shows zero. The documentation says so. An override is a follow-up issue,
  only if the owner's wallets need it.
- Kaspa extended keys. Registration keeps refusing them with `extended_key`.
- Per-address balances or a per-address view in the API or the UI. The derived addresses
  are internal. Only their sum is served.
- Per-address isolation of a failure. One failed read still fails the whole chain, as today
  (#54).
- A configurable gap limit. It is a domain constant.
- Detecting overlap between a derived address and a separately registered address wallet.
  Both would be counted. Reconciliation would report the mismatch. The documentation says
  so.
- Hardened derivation, which needs a private key.

## Rulings

- **R1. The key lives in the existing address columns, and a new `kind` column says what
  it is.** This reuses the unique constraint, archiving and labels unchanged.
  - `address_display` holds the key exactly as entered, after stripping whitespace. Base58
    is case-sensitive, so there is no case folding. The API masks this form.
  - `address_canonical` holds the key **re-serialised at depth 0**:
    `version || 0x00 || 00000000 || 00000000 || chain_code || public_key`.
    - Without this, one account exported twice by tools that serialise depth, parent
      fingerprint or child number differently would be two strings, two wallets, the same
      addresses, and a silently doubled total. Review finding S1 (2026-10-03).
    - The version stays in the canonical form, because an `xpub` and a `zpub` over the same
      bytes derive different addresses and are legitimately different wallets.
    - The re-serialised key is valid under R4, and it is what the provider is handed.
  - Rejected: a separate nullable `extended_key` column. It needs a CHECK that exactly one
    of the two is set, and every reader would have to branch on kind anyway.
- **R2. The script type is fixed by the prefix** (SLIP-0132 version bytes):

  | Prefix | Version | Network family | Script |
  |---|---|---|---|
  | `xpub` | `0x0488B21E` | main | P2PKH (BIP44) |
  | `ypub` | `0x049D7CB2` | main | P2SH-P2WPKH (BIP49) |
  | `zpub` | `0x04B24746` | main | P2WPKH (BIP84) |
  | `tpub` | `0x043587CF` | test | P2PKH |
  | `upub` | `0x044A5262` | test | P2SH-P2WPKH |
  | `vpub` | `0x045F1CF6` | test | P2WPKH |

  **Refusals by prefix, before any decoding**, so that a key with a typo is still named for
  what it is:
  - a string that starts with `xprv`, `yprv`, `zprv`, `tprv`, `uprv`, `vprv`, `Yprv`, `Zprv`,
    `Uprv` or `Vprv` is refused with a new reason, `private_key`;
  - one that starts with `Ypub`, `Zpub`, `Upub` or `Vpub` is refused with a new reason,
    `extended_key_multisig`.

  After decoding, a version outside the six above is `unknown_version_byte`. For an 82-byte
  payload the version fixes the four-character prefix, so no separate "multisig version"
  check is reachable after decoding.
- **R2a. Kaspa** (ruled 2026-10-03, during implementation). On Kaspa, `classify_wallet_key`
  refuses any of the 20 prefixes above by prefix, before the Kaspa codec runs. A private one
  is refused as `private_key`. A public one, single-sig or multisig, is refused as
  `extended_key`. Everything else goes to the Kaspa codec unchanged. Before this ruling, a
  `tpub` on Kaspa reached the codec and was refused as `mixed_case`, which said nothing
  useful.
- **R2b. What counts as a private key** (review finding D1, 2026-10-03). A value is refused
  as `private_key` when either of these holds:
  - after surrounding whitespace and Unicode format characters (category `Cf`: zero-width
    space, word joiner, directional marks, BOM) are removed, it starts with a private prefix;
  - **anywhere in it**, after the same `Cf` removal (so that an invisible character inside
    the body cannot split it), there is a private-key-shaped run,
    `(?<![1-9A-HJ-NP-Za-km-z])(?:[xyztuv]|[YZUV])prv[1-9A-HJ-NP-Za-km-z]{100,}`.
    - This catches a key pasted after other text, inside quotes, or behind an invisible
      character.
    - **The left boundary is required.** An extended *public* key has a 107-character Base58
      body. Without the boundary, roughly one public key in a few hundred thousand has a
      private prefix inside its body and would be refused. Found by the tester on 2026-10-03.
    - No address of either chain contains a Base58 run of 100 or more characters.
    - The one case it gives up: a private key glued directly onto preceding Base58 text, with
      no separator, or with only a format character as the separator.

  Where the test applies:
  - **The server** runs it on the raw value, before stripping and before the length cap, so
    an over-long string is still named `private_key`.
  - **The client** runs the same test on the address and the label as they are entered.

  Removing the `Cf` characters is for this test only. A stored value is never changed by it,
  and an address that contains one is still refused as `invalid_character`.
- **R3. The network check stays at read time,** as for addresses (spec 007).
  - Registration accepts either family.
  - The provider refuses a key whose family does not match `PORTFOLIO_BITCOIN_NETWORK`
    with `wrong_network` before any request, which lands as `address_rejected`.
  - Derived addresses are encoded for the configured network: `bc` with versions
    `0x00`/`0x05` on mainnet; `tb` with `0x6F`/`0xC4` on testnet; `bcrt` with
    `0x6F`/`0xC4` on regtest.
- **R4. The depth is not enforced.** Electrum exports a depth-1 key, and BIP44 account keys
  are depth 3. Derivation is always `/branch/index` below the key as given. The BIP32
  structural rules still apply: depth 0 requires a zero parent fingerprint and a zero child
  number.
- **R5. The scan.**
  - `GAP_LIMIT = 20` per branch.
  - A branch is complete when its last 20 addresses, by index, are unused, counted after its
    highest used index. A branch with no used address is complete at 20 addresses.
  - **Used** means `chain_stats.tx_count > 0`, or `mempool_stats.tx_count > 0` when
    `mempool_stats` is present. Once persisted as used, an address stays used.
  - Every sync reads every persisted address of the wallet, used or not. Funds can arrive
    at any of them, and address reuse is real. It then derives and reads only the indices
    needed to complete each branch.
  - `MAX_ADDRESSES_PER_BRANCH = 1000`. A branch that would pass it fails the read with
    `ProviderResponseError` and a fixed message. The only plausible cause in a single-owner
    tracker is a vendor reporting history for every address, which is an answer we cannot
    use. It must not become an endless scan.
  - BIP32 says that an index whose derivation is invalid (`IL >= n`, or the point at
    infinity) has no key. That index is skipped. It is not persisted and does not count
    toward the gap.
- **R6. "Incremental" means no re-derivation.**
  - The scan is handed the persisted addresses and derives only indices above the highest
    persisted one on each branch.
  - New addresses and newly used flags are written in the same per-chain commit as the
    snapshot.
  - An interrupted scan persists nothing, so the next sync starts again from what was last
    committed. That only costs time.
- **R7. The sum.**
  - `confirmed` is the sum over every scanned address.
  - `pending` is the sum when every address reported one; when any of them reported `None`,
    `pending` is `None`. This is the existing meaning of a missing `mempool_stats`.
  - A wallet whose scan succeeded with no used address has a real zero snapshot, not
    `unread`.
- **R8. Masked in the API.**
  - For `kind = "extended_key"`, `WalletResponse.address` is the first four characters,
    `…` (U+2026), then the last four characters. This applies to list, create and patch.
  - The full key is never served.
  - The frontend shows the masked form with no copy button.
- **R9. Pure-Python cryptography in `domain/`, with no new dependency.**
  - secp256k1 point decompression, addition and scalar multiplication, plus RIPEMD-160.
    Everything handled is public data, so constant-time execution is not a requirement.
  - RIPEMD-160 is not taken from `hashlib`. `hashlib.new("ripemd160")` depends on the
    OpenSSL build (OpenSSL 3.0.0 to 3.0.6 moved it to the legacy provider), and nothing
    proves the production image's build before merge.
  - Rejected: `coincurve`, `embit`, `bip32` and `bip_utils`. Each adds a supply-chain
    dependency for about 200 lines that the published test vectors pin exactly.
- **R10. The scan lives in the provider. The bookkeeping lives in the service.**
  - Derivation needs the configured network (R3), which only the provider knows.
  - The service owns persistence, and keeps the database out of the gather.
  - The gap-limit arithmetic is a pure domain function, so it can be property-tested on its
    own.
- **R11. No mainnet extended key or mainnet address is ever in the repository, as a literal
  or assembled at run time.**
  - Mainnet support is proven on the version and encoding tables themselves (R2, R3). Every
    end-to-end vector is in test-network form.
  - The BIP32 vectors appear as `tpub`. BIP49's published vector is already testnet. BIP84's
    vector appears as `vpub`, with its addresses as `tb1` over the same witness programs.
  - The conversion runs at run time, reading the published vectors or deriving them from
    their published seeds. **It writes no mainnet literal to any file, scratchpad included.**
    Only the testnet forms are committed.
- **R12. The migration is reversible, with one refusal.** The downgrade refuses while any
  `kind = 'extended_key'` wallet exists. The downgraded application would read the key as
  an address and fail the Bitcoin chain on every tick. With no such wallet, it drops the
  table and the column.
  - Archived wallets count too, because archiving keeps the row.
  - No endpoint or command deletes a wallet row. `docs/operations.md` gives the operator's
    SQL, run after `python -m portfolio backup`: `PRAGMA foreign_keys = ON`, then delete the
    extended-key wallets, which cascades to their snapshots and derived addresses.
  - Those wallets' history is lost, but the old schema could not read it anyway. The backup
    is the undo.
- **R13. A latent validator defect, fixed** (found 2026-10-03, during implementation).
  - **The defect.** `_looks_like_bech32` sends some valid testnet P2PKH addresses to the
    bech32 decoder: an `m` or `n` address whose last `1` follows only letters and precedes
    only bech32-charset characters. The decoder then refused them as `mixed_case`. Derived
    `tpub` addresses hit this often. Mainnet `1`/`3` and testnet `2` addresses cannot, so no
    registered production address was ever affected.
  - **The fix.** `validate_bitcoin_address` and `bitcoin_network_of` fall back to
    Base58Check when the bech32 reading fails, and re-raise the original bech32 reason when
    Base58Check fails too.
  - **No existing reason changes.** The fallback only turns a refusal into an acceptance,
    and only for a string that verifies as Base58Check with a known version byte.
  - **Rejected:** restricting the hrp to `bc`/`tb`/`bcrt`. That would change the pinned
    `unknown_prefix` reason for foreign hrps.

## Design

### Domain (pure; `hashlib` and `hmac` are allowed, `secrets` and `os` are not)

- `domain/secp256k1.py`: the curve constants; `decompress(bytes33) -> Point`, which refuses
  a prefix other than `02`/`03` or an x coordinate that is not on the curve; `add`;
  `multiply_generator(k)`; and `compress(Point) -> bytes`.
- `domain/ripemd160.py`: `ripemd160(data: bytes) -> bytes`.
- `domain/extended_keys.py`:
  - The R2 version table, and `ExtendedPublicKey(network_family, script_type, depth,
    parent_fingerprint, child_number, chain_code, public_key)`.
  - `parse_extended_public_key(raw)`. It applies the R2 prefix refusals, then a Base58Check
    decode of exactly 82 bytes, a known version, the depth-0 rules, and a public key on the
    curve. It raises `AddressInvalidError` with one of: `private_key`,
    `extended_key_multisig`, `bad_checksum`, `unknown_version_byte`, `malformed`, or a new
    `invalid_public_key`.
  - `derive_child(key_or_branch, index) -> DerivedKey | None`, non-hardened only. It returns
    `None` for R5's invalid index.
  - `address_of(public_key, script_type, network) -> str` (R3 encodings).
  - `GAP_LIMIT`, `MAX_ADDRESSES_PER_BRANCH`, and `addresses_to_extend(used_by_index) -> int`
    (R5).
- `domain/addresses.py`:
  - Generalise `base58check_decode` over the payload length, keeping the 25-byte behaviour
    for addresses.
  - Add `base58check_encode`, a segwit address encoder, and `_convert_bits` with padding.
  - Add the new `AddressRejection` members with their fixed sentences, none of which
    interpolates anything.
  - `validate_bitcoin_address` keeps refusing extended keys. Registration routes them
    elsewhere (below).
  - **Every derived address must validate through `validate_bitcoin_address`, and come out
    unchanged as its own canonical form.**
- `domain/chains.py`:
  - `classify_wallet_key(chain_key, raw) -> WalletKey(kind, canonical, display)`.
  - On Bitcoin, any string with an extended-key prefix, public or private, single- or
    multisig, goes to the extended-key parser. Everything else goes to `validate_address`.
  - On Kaspa, the R2a prefix refusals apply, and everything else is unchanged.
  - `WalletKind(StrEnum)`: `address` and `extended_key`.

### Data model (migration `0011_extended_keys`, on `0010_exchange_balances`)

- `wallets.kind TEXT NOT NULL DEFAULT 'address'`.
  - `CHECK (kind IN ('address', 'extended_key'))`.
  - `CHECK (kind = 'address' OR chain_key = 'bitcoin')`.
  - Existing rows become `address`.
- New table `derived_addresses`:

  | Column | Type | |
  |---|---|---|
  | `id` | INTEGER PK | |
  | `wallet_id` | FK `wallets.id`, `ON DELETE CASCADE`, NOT NULL | |
  | `branch` | INTEGER NOT NULL | `CHECK (branch IN (0, 1))` |
  | `child_index` | INTEGER NOT NULL | `CHECK (child_index >= 0 AND child_index < 2147483648)` |
  | `address_canonical` | TEXT NOT NULL | |
  | `used` | BOOLEAN NOT NULL | |
  | `created_at` | `UtcDateTime` NOT NULL | |

  - `UNIQUE (wallet_id, branch, child_index)`.
  - `CHECK (used IN (0, 1))`, named `ck_derived_addresses_used`. This follows the existing
    convention for Boolean columns, because SQLite's Boolean emits no CHECK of its own.
  - An offline `alembic downgrade --sql` past 0011 refuses outright. With no database to
    count extended-key wallets in, a script could strand one (R12). Nothing in this
    repository runs migrations offline.
  - CHECK texts are model constants, repeated verbatim in the migration as 0010 does. The
    `batch_alter_table` on `wallets` uses an explicit `copy_from`.
- The downgrade follows R12.
- `repositories/derived_addresses.py`: `list_for_wallets(wallet_ids)`, plus an `apply(...)`
  that inserts new rows and sets `used`. It never sets `used` back to false.
- `repositories/wallets.py`: `add` takes `kind`. `list_all_active` returns it.

### Provider (`providers/base.py`, `providers/chains/bitcoin.py`)

- `parse_address_response` additionally reads `tx_count` from `chain_stats` and, when
  present, from `mempool_stats`. Each must be a non-negative integer, or the response is
  refused. `AddressStats` gains `used: bool`. `AddressBalance` and `fetch_balances` are
  unchanged.
- In `providers/base.py`:
  - `KnownDerivedAddress(branch, index, address, used)`;
  - `ScannedAddress(branch, index, address, used, confirmed, pending)`;
  - `ExtendedKeyScan(addresses, decimals)`;
  - a `runtime_checkable` Protocol, `ExtendedKeyScanner.scan_extended_key(key: str, known:
    Sequence[KnownDerivedAddress]) -> ExtendedKeyScan`.
- `EsploraProvider.scan_extended_key`:
  - parse the key, and apply the R3 network check before any request;
  - derive each branch key once;
  - per branch, in index order, read every known address, then extend with
    `addresses_to_extend` until it returns 0, subject to R5's cap;
  - read sequentially through the same per-address path, endpoint label and client as
    `fetch_balances`, so every request, retries included, acquires the host limiter;
  - log nothing, which is this module's existing contract. The service logs per-wallet
    counts by `wallet_id`, never a key, an address or an index;
  - validate the persisted addresses it is handed before any request, exactly as registered
    addresses are validated: codec, canonical equality, network.
    - Under regtest, a Base58 derived address reads as testnet because the bytes are
      identical, and that is expected.
    - A switch of `PORTFOLIO_BITCOIN_NETWORK` between testnet and regtest under an existing
      `vpub` wallet fails the chain as `address_rejected`: its `tb1`/`bcrt1` addresses
      differ by network. `tpub` and `upub` addresses are byte-identical on both, so those
      wallets keep reading correctly (review N1).
    - The remedy, in `docs/operations.md`, is to take a backup, then
      `DELETE FROM derived_addresses`. The wallets, their labels and their snapshot history
      stay, and the next sync re-derives every key for the new network at first-scan cost.
    - Removing the wallet and adding it again does not work: archiving keeps the row's
      unique slot, so the re-add is a 409, and restoring brings the stale rows back.
  - A branch key (`/0` or `/1`) that BIP32 gives no key for is refused as
    `invalid_public_key`. BIP44 fixes the branch index, so there is no next one to move to.
  - A malformed persisted position is a programming error and raises `ValueError`, which the
    sync records as `internal`. That covers a branch other than 0 or 1, an index outside
    `[0, 2^31)`, and a duplicate. The database's CHECK and UNIQUE make it unreachable.
- **No new module goes in `providers/chains/`.** `test_chain_modules.py` requires every
  module there to register a provider.

### Service

- `services/wallets.py`:
  - `create_wallet` uses `classify_wallet_key` and stores `kind`.
  - `WalletView` gains `kind`, and `address` follows R8.
  - The duplicate check is unchanged, on the canonical form.
- `services/balance_sync.py`:
  - Before the gather, load the derived addresses of every active extended-key wallet.
  - In `_read_chain`, address wallets are read as today. Then each extended-key wallet of
    the chain is scanned in turn.
  - A chain whose provider is not an `ExtendedKeyScanner` but has an extended-key wallet
    fails as `internal`. The domain makes that unreachable, and the check makes it loud.
  - `_write_chain` writes the R7 sum as the wallet's snapshot, plus the new and newly used
    derived addresses, in the existing per-chain commit.
  - Any failure fails the chain as today.
  - Run counts stay counts of wallets.
  - **A scan's result is checked like `_fan_out` checks a batch.** It must cover every
    persisted position, with no position twice. Otherwise the sync raises
    `ProviderResponseError` and the chain fails as `response`. A dropped address would make
    the sum quietly wrong.
  - The `address_rejected` detail sentence now says "an address or an extended key was
    refused before it was read". Extended-key wallets are scanned after the address batch,
    so "before any request" was no longer true.
  - `balance_sync_extended_key_scanned` carries `wallet_id` and three counts. It is logged
    after the commit: at INFO when the scan persisted new addresses, at DEBUG otherwise.

### API

- No new endpoint. Nothing is added to `PUBLIC_API_PATHS`.
- `POST /api/wallets` accepts an extended public key in `address` for `chain_key: bitcoin`.
  A refusal is the existing 422, with `loc=("body","address")` and the reason as `type`:
  `private_key`, `extended_key_multisig`, `invalid_public_key`, `extended_key` (on Kaspa),
  or an existing decode reason.
- `WalletResponse` gains `kind: "address" | "extended_key"`. For an extended key, `address`
  is masked (R8).

### Logging and secret scanning

- `logging.py`: the value pattern for extended keys also covers the private prefixes from
  R2, so a private key that reaches a log is redacted like a public one. The key-name
  prefixes gain them too.
- `.gitleaks.toml`: a new `extended-private-key` rule over every private prefix, mainnet and
  test. A private key has no business in the repository, testnet included. The full-history
  scan must still pass, and the tester's positive control must show that the rule fires.

### Frontend

- `lib/chains.ts`:
  - **On Bitcoin, a public extended-key prefix** replaces today's "only single addresses"
    hint with an informative one: every address of the wallet will be scanned, and the
    first scan takes about a minute.
  - **A private key is refused as it is entered**, in the address field and in the label
    field, on any chain. The test is R2b's.
    - The field is cleared at once (`flushSync`), and focus moves to the alert
      (`tabIndex={-1}`). The rest of a hand-typed key then lands on a non-editable element
      instead of the emptied field.
    - The alert says it is a private key that must not be entered anywhere.
    - The value is never sent. There is no submit-time guard, because none is reachable.
  - **Kaspa:** unchanged.
- Wallet list and dashboard table: an extended-key wallet shows the masked `address`, a small
  "Extended key" label, and no copy button. `Address.tsx` must not truncate the masked form
  a second time.
- `WalletForm` maps the new 422 reasons to field messages.
- `api/generated/schema.ts` is regenerated after the backend schema settles (drift check).

### Docs

- `docs/providers.md`, a new section, *Extended public keys*:
  - Why the public APIs cannot answer this, with the date the vendors' documentation was
    checked.
  - What is derived and the script type for each prefix.
  - The gap limit and the cap.
  - The cost: at least 40 requests on a first scan, at least 1 s apart per host.
  - That `used` comes from `tx_count`.
  - The `xpub` P2PKH caveat (re-export as `zpub`/`ypub`).
  - **`docs/providers.md` never spells a mainnet prefix.** Its existing guard
    (`tests/providers/test_documentation.py`) forbids the bare substrings, `bc1q` included,
    because it is the document a provider author copies from. So its table gives the mainnet
    version bytes, and it points to `docs/operations.md`, which names the prefixes for the
    owner. The guard stays as strict as it is.
  - No taproot and no multisig.
- `docs/operations.md`: adding an extended key; the first scan's duration; the overlap
  caveat; and the downgrade refusal.

## Acceptance criteria

The issue's criteria, verbatim, each followed by its interpretation where one is needed.

1. **xpub, ypub and zpub parsed and validated offline.**
   - So are `tpub`, `upub` and `vpub`.
   - "Validated" means the checksum, a known version, the depth-0 rules, and a public key on
     the curve.
   - Multisig and private keys are refused by name (R2).
   - Mainnet support is proven on the version table (R11).
2. **The correct script type is derived per prefix.** Per R2, against published vectors in
   test-network form (R11), and on every network encoding of R3.
3. **A gap-limit-20 scan finds all funded addresses in a testnet fixture.**
   - The fixture has used addresses at receive indices 0, 5 and 24, and change indices 0
     and 3. All of them are found.
   - A used receive address at index 46 (more than 20 past 24) is not reached, by design.
4. **Rescans are incremental, not a full re-derivation** (R6). A second sync with no new use
   derives nothing. A newly used address at index k derives only the indices from the
   highest persisted one up to k + 20.
5. **Wallet balance is the sum of derived-address balances** (R7), including `pending`'s
   `None` rule and the real zero.
6. **Derivation respects the chain rate limiter.** Every derived-address request goes
   through the shared transport's `HostRateLimiter`. This is proven with the real transport
   and an injected clock, not with a mock of the limiter.
7. **Extended keys are redacted everywhere.**
   - Logs, by value, both public and private forms, including from `httpx` at DEBUG during a
     scan.
   - The API (R8).
   - Error messages, which are fixed sentences.
   - Never in any response body in full.
8. **Fixtures use testnet extended keys only.** The existing scanners pass, and the new
   gitleaks rule's positive control fires.
9. **`docs/providers.md` records that public-API xpub lookup is unavailable and why.**

Criteria added by this spec:

10. The migration upgrades over existing data. Its downgrade refuses while an extended-key
    wallet exists, and otherwise round-trips (R12).
11. The frontend accepts an extended key, refuses a private one before sending it, and
    shows the masked form, with no copy button, at 1280 px and 375 px.
12. The float ban, the layering contracts, the allowlist pin and the OpenAPI drift check all
    pass. The coverage floors are unchanged. Domain coverage stays at or above 95/90 with
    the new modules.
13. The full gate passes.

## Test plan

| # | Criterion | Tests |
|---|---|---|
| 1 | Parsing | `tests/domain/test_extended_keys.py`: each test prefix parses; each refusal reason, including a `tprv` refused by prefix (short, so it is not key-shaped); a version-table test asserting all six R2 versions as integers; depth-0 rules; off-curve key; a property test that a random corruption of a valid key never parses as a different valid key |
| 1 | Curve and hash | `tests/domain/test_secp256k1.py` (decompression of known points, refusal of non-points, `multiply_generator` against known multiples, the group law on samples); `tests/domain/test_ripemd160.py` (the published vectors, plus a property test against `hashlib` where it is available, skipped otherwise) |
| 2 | Script types | `tests/domain/test_derivation_vectors.py`: the BIP32 vectors' non-hardened public children as `tpub`; BIP49's testnet vector; BIP84's vector as `vpub`/`tb1`; `address_of` on each of R3's networks; a property test that every derived address validates and is canonical |
| 3 | Gap scan | `tests/providers/test_bitcoin_extended_key_scan.py`: a fake Esplora answering by address. Includes the `tx_count` parsing and refusals, the cap, the skipped invalid index (by injection), and the R3 refusal before any request |
| 3 | Gap arithmetic | `tests/domain/test_gap_limit.py`: unit and property tests of `addresses_to_extend` |
| 4 | Incremental | `tests/services/test_balance_sync_extended_keys.py`: a spy on derivation, two syncs, then a newly used index |
| 5 | Sum | same file: the confirmed sum, the pending `None` rule, the real zero, the snapshot read by the dashboard |
| 6 | Limiter | `tests/providers/test_bitcoin_extended_key_scan.py`: the real `RetryingTransport` and `HostRateLimiter`, an injected clock, and one acquire per request, retries included |
| 7 | Redaction | `tests/security/`: the sentinel-style scan with DEBUG on (no `tpub` and no derived address on stdout); a private-key value redacted; `tests/api/test_wallets_router.py` for the masked `address` on list, create and patch, with the full key nowhere in any body |
| 8 | Fixtures | the existing `test_address_logging.py` and secret-scan tests; the gitleaks positive control in the scratchpad |
| 9 | Docs | `tests/test_extended_keys_documentation.py`, in the style of the existing documentation tests |
| 10 | Migration | `tests/db/test_extended_keys_migration.py`, in the style of `test_exchange_balances_migration.py` |
| 11 | Frontend | `WalletsPage.test.tsx` (hint, private-key block with no request sent, the new 422 reasons), list and dashboard tests for the masked form; the tech lead's browser check |
| 12–13 | Gate | `python scripts/check.py`, the OpenAPI drift comparison, diff-cover |

## File ownership

| Agent | Owns |
|---|---|
| backend-dev | `backend/src/portfolio/**`, `.gitleaks.toml`, `docs/providers.md`, `docs/operations.md` |
| frontend-dev | `frontend/src/**` except tests, including the regenerated `api/generated/schema.ts` |
| tester | `backend/tests/**`, `frontend/src/**/*.test.ts(x)`, the frontend test fixtures |

## Risks

- **Hand-written elliptic-curve and hash code.**
  - Mitigated by the published vectors, the property tests against the existing decoder,
    and mutation testing.
  - Only public data is involved, so a bug can produce a wrong address but cannot leak a
    secret. A wrong address would show as a zero balance that reconciliation flags.
- **Speed on the Pi.**
  - A pure-Python scalar multiplication takes milliseconds. A first scan derives at least 40
    addresses, and its requests are at least 1 s apart anyway.
  - The tester times 100 derivations locally and records the figure.
- **An `xpub` exported for a segwit account.** Some wallets do this. It derives P2PKH and
  shows zero. This is documented, and an override is a follow-up only if needed.
- **The vendors' lack of an xpub endpoint** is the issue's claim. backend-dev confirms it
  against the live documentation and records the date, or records that it could not be
  confirmed.
- **1 s per host is a guess** (`docs/providers.md`). An extended key adds at least 40
  requests per 15-minute sync. That is well inside what a 1 s spacing allows, but it is more
  traffic to the same vendor.
- **The `tx_count` fields** are documented but have not been read by this code before. If
  either is absent from a live response, the read fails loudly as `response`. It does not
  silently count as unused.
- **A scan that hits the cap persists nothing,** because the chain rolls back. A vendor that
  reports every address as used would therefore cost 1000 requests on every sync. At the
  1 s per-host floor that is about 17 minutes, longer than the 15-minute interval. Bitcoin
  stays failed, which is loud, and the balance timer reports `late`. The behaviour is
  accepted. A cheaper failure is a follow-up, if this is ever seen.
