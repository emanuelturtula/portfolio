/**
 * The chain registry the wallet form and the balance rows both read from: a display name
 * per chain, a format hint for the empty-address state, and a pure function offering at
 * most one advisory hint for whatever the owner has typed so far.
 *
 * **Advisory only, with one exception.** `domain/addresses.py` verifies bech32, bech32m,
 * Base58Check and Kaspa's CashAddr offline, and a TypeScript copy of those codecs would be a
 * second implementation that can disagree with the first. So this module checks nothing a
 * checksum would - it only recognises shapes closely enough to point the owner at the right
 * control - and never blocks a submission. The server's checksum is the only verdict; see
 * docs/specs/011-wallets-page-value-dashboard.md.
 *
 * The exception is a private extended key (`xprv` and its relatives), recognised by
 * `isPrivateExtendedKey`, on any chain and in any field. The hint for one is
 * `blocksSubmission`: sending one to the server at all would be the harm, so the refusal has
 * to happen before the request exists, not after the server has seen the value. See
 * docs/specs/031-bitcoin-extended-keys.md, R2b.
 *
 * `Record<ChainKey, ChainInfo>` below is total by construction: a chain added to the
 * generated `ChainKey` union without an entry here fails `tsc`, the same guarantee
 * `CHAIN_VALIDATORS` gives the backend for the same reason.
 */
import type { components } from '@/api/generated/schema';

export type ChainKey = components['schemas']['ChainKey'];

export interface ChainInfo {
  readonly displayName: string;
  /** Shown when the address field is empty, and used to build the "empty" hint. */
  readonly formatHint: string;
}

export const CHAINS: Record<ChainKey, ChainInfo> = {
  bitcoin: {
    displayName: 'Bitcoin',
    formatHint:
      'Starts with bc1, 1 or 3 - or tb1, m, n or 2 on testnet. An extended public key (xpub, ypub or zpub) is also accepted.',
  },
  kaspa: {
    displayName: 'Kaspa',
    formatHint: 'Starts with kaspa: - or kaspatest: on testnet. The prefix is part of the address.',
  },
};

/** Every chain, in the order the form's chain selector lists them. */
export const CHAIN_KEYS = Object.keys(CHAINS) as ChainKey[];

function isChainKey(value: string): value is ChainKey {
  return Object.hasOwn(CHAINS, value);
}

/**
 * A chain's display name, falling back to the raw key.
 *
 * `WalletResponse.chain_key` and `WalletBalanceResponse.chain_key` are typed `string` in
 * the generated schema, not `ChainKey` - the backend publishes them that way deliberately,
 * see `api/schemas/wallets.py` - so a chain this build does not know about must still
 * render something rather than throwing.
 */
export function chainDisplayName(chainKey: string): string {
  return isChainKey(chainKey) ? CHAINS[chainKey].displayName : chainKey;
}

/** Single-signature public extended keys: SLIP-0132 mainnet forms and their test-network twins. */
const EXTENDED_PUBLIC_PREFIXES = ['xpub', 'ypub', 'zpub', 'tpub', 'upub', 'vpub'] as const;

/**
 * Private extended key prefixes. Compared against the lower-cased value, so the capitalised
 * multisig forms (`Yprv`, `Zprv`, `Uprv`, `Vprv`) are covered by their lower-case twin. No
 * address of either chain starts with any of these, so matching without regard to case cannot
 * refuse a legitimate address.
 */
const EXTENDED_PRIVATE_PREFIXES = ['xprv', 'yprv', 'zprv', 'tprv', 'uprv', 'vprv'] as const;

const BITCOIN_EXTENDED_KEY_HINT =
  'This is an extended public key. Every address of this wallet will be scanned, and the first scan takes about a minute.';

const UNSUPPORTED_EXTENDED_KEY_HINT =
  'This is an extended public key. Only single addresses are supported.';

/** What every refusal of a private key says first, whichever field it was entered in. */
export const PRIVATE_KEY_WARNING =
  'This is a private key. Never enter a private key here or anywhere else.';

const PRIVATE_KEY_MESSAGE = `${PRIVATE_KEY_WARNING} Enter the extended public key instead.`;

/**
 * Unicode format characters (category `Cf`): zero-width space, word joiner, directional
 * marks, the byte-order mark. Rich-text copies add them, and a prefix test that stops at
 * the first one is a test that a pasted key walks around.
 */
const FORMAT_CHARACTERS = /\p{Cf}/gu;

/**
 * A private-key-shaped run, wherever it sits: a prefix, then 100 or more Base58 characters.
 * No address of either chain contains a Base58 run that long, and it is what catches a key
 * pasted after other text, or inside quotes.
 *
 * The prefix must not be preceded by a Base58 character. Without that, the pattern would
 * also match inside a genuine extended public key, whose 107-character body contains a
 * private prefix by chance in roughly one public key in a few hundred thousand - and refuse
 * its owner.
 *
 * Exported so a test can prove its prefix classes on the pattern itself, without writing a
 * key-shaped string for every prefix.
 */
export const PRIVATE_KEY_RUN =
  /(?<![1-9A-HJ-NP-Za-km-z])(?:[xyztuv]|[YZUV])prv[1-9A-HJ-NP-Za-km-z]{100,}/;

/**
 * Whether a value is, or contains, a private extended key (spec 031, R2b). True when either
 * holds:
 *
 * - after surrounding whitespace and Unicode format characters are removed, it starts with a
 *   private prefix, compared case-insensitively;
 * - anywhere in it, there is a private-key-shaped run.
 *
 * Removing the format characters is for this test alone. Nothing here changes what is stored
 * or sent for a value that is not refused.
 */
export function isPrivateExtendedKey(value: string): boolean {
  const visible = value.replace(FORMAT_CHARACTERS, '');
  const lower = visible.trim().toLowerCase();
  return (
    EXTENDED_PRIVATE_PREFIXES.some((prefix) => lower.startsWith(prefix)) ||
    PRIVATE_KEY_RUN.test(visible)
  );
}

export interface AddressHint {
  readonly message: string;
  /** Present exactly when the hint offers a control that switches the selected chain. */
  readonly switchTo?: ChainKey;
  /**
   * Present, and `true`, exactly when what was typed must be refused outright: not kept in
   * the form, and never submitted. Today that is a private extended key and nothing else.
   */
  readonly blocksSubmission?: true;
}

function looksLikeKaspaAddress(trimmed: string): boolean {
  const lower = trimmed.toLowerCase();
  return lower.startsWith('kaspa:') || lower.startsWith('kaspatest:');
}

function looksLikeBitcoinAddress(trimmed: string): boolean {
  const lower = trimmed.toLowerCase();
  return (
    lower.startsWith('bc1') ||
    lower.startsWith('tb1') ||
    lower.startsWith('bcrt1') ||
    /^[13mn2]/.test(trimmed)
  );
}

/**
 * At most one advisory hint for a typed address, per the table in the spec's "Chain hints
 * are advisory" section. Advice only, with one exception: a hint with `blocksSubmission` (a
 * private extended key) is a refusal, and a caller must not keep or send the value it was
 * computed from. Every other hint never disables submission - callers must not read the
 * absence of a hint as "valid", only as "nothing to point out yet".
 */
export function addressHint(chainKey: ChainKey, typed: string): AddressHint | undefined {
  const trimmed = typed.trim();

  if (trimmed === '') {
    return { message: CHAINS[chainKey].formatHint };
  }

  // First of all, and on every chain: a private key is never the right thing to type here,
  // whichever chain is selected.
  if (isPrivateExtendedKey(typed)) {
    return { message: PRIVATE_KEY_MESSAGE, blocksSubmission: true };
  }

  const lower = trimmed.toLowerCase();

  if (chainKey === 'bitcoin') {
    // Base58 is case-sensitive, so this compares the value as typed: only the lower-case
    // prefixes are single-signature keys. `Ypub` and its relatives are multisig, which the
    // server refuses by name; promising a scan for one would be wrong.
    if (EXTENDED_PUBLIC_PREFIXES.some((prefix) => trimmed.startsWith(prefix))) {
      return { message: BITCOIN_EXTENDED_KEY_HINT };
    }
  } else if (EXTENDED_PUBLIC_PREFIXES.some((prefix) => lower.startsWith(prefix))) {
    return { message: UNSUPPORTED_EXTENDED_KEY_HINT };
  }

  if (chainKey === 'bitcoin' && looksLikeKaspaAddress(trimmed)) {
    return { message: 'This looks like a Kaspa address.', switchTo: 'kaspa' };
  }

  if (chainKey === 'kaspa' && looksLikeBitcoinAddress(trimmed)) {
    return { message: 'This looks like a Bitcoin address.', switchTo: 'bitcoin' };
  }

  if (chainKey === 'kaspa' && !trimmed.includes(':')) {
    return {
      message: 'Kaspa addresses include their network prefix, for example kaspa: or kaspatest:.',
    };
  }

  return undefined;
}
