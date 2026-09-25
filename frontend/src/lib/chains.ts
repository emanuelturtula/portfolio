/**
 * The chain registry the wallet form and the balance rows both read from: a display name
 * per chain, a format hint for the empty-address state, and a pure function offering at
 * most one advisory hint for whatever the owner has typed so far.
 *
 * **Advisory only.** `domain/addresses.py` verifies bech32, bech32m, Base58Check and
 * Kaspa's CashAddr offline, and a TypeScript copy of those codecs would be a second
 * implementation that can disagree with the first. So this module checks nothing a
 * checksum would - it only recognises shapes closely enough to point the owner at the
 * right control - and never blocks a submission. The server's checksum is the only
 * verdict; see docs/specs/011-wallets-page-value-dashboard.md.
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
    formatHint: 'Starts with bc1, 1 or 3 - or tb1, m, n or 2 on testnet.',
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

const EXTENDED_KEY_PREFIXES = ['xpub', 'ypub', 'zpub', 'tpub', 'upub', 'vpub'] as const;

export interface AddressHint {
  readonly message: string;
  /** Present exactly when the hint offers a control that switches the selected chain. */
  readonly switchTo?: ChainKey;
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
 * are advisory" section. Never disables submission - callers must not read the absence of
 * a hint as "valid", only as "nothing to point out yet".
 */
export function addressHint(chainKey: ChainKey, typed: string): AddressHint | undefined {
  const trimmed = typed.trim();

  if (trimmed === '') {
    return { message: CHAINS[chainKey].formatHint };
  }

  const lower = trimmed.toLowerCase();
  if (EXTENDED_KEY_PREFIXES.some((prefix) => lower.startsWith(prefix))) {
    return { message: 'This is an extended public key. Only single addresses are supported.' };
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
