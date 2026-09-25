import { describe, expect, it } from 'vitest';

import { addressHint, CHAIN_KEYS, chainDisplayName, CHAINS } from '@/lib/chains';
import { ADDRESSES } from '@/test/fixtures';

/**
 * The hint table from the spec, one `describe` per row:
 *
 * | Typed | Hint |
 * |---|---|
 * | empty | the chain's format hint |
 * | an extended key prefix | only single addresses are supported |
 * | a Kaspa prefix while Bitcoin is selected | looks like Kaspa, with a switch |
 * | a Bitcoin shape while Kaspa is selected | looks like Bitcoin, with a switch |
 * | Kaspa selected, no `:` | Kaspa addresses include their network prefix |
 *
 * Mainnet shapes appear here only as prefixes far too short to be an address
 * (`bc1q`, `1A`, `kaspa:qq`, `xpub6`). The gitleaks rules match full-length
 * mainnet addresses, and none of these is one.
 */

describe('chain registry', () => {
  it('lists both chains the backend knows, in a stable order', () => {
    expect(CHAIN_KEYS).toEqual(['bitcoin', 'kaspa']);
  });

  it('names each chain for a person', () => {
    expect(chainDisplayName('bitcoin')).toBe('Bitcoin');
    expect(chainDisplayName('kaspa')).toBe('Kaspa');
  });

  it('falls back to the raw key for a chain this build does not know', () => {
    // `chain_key` is typed `string` on the wire. A chain added on the backend
    // before this build ships must still render as something.
    expect(chainDisplayName('litecoin')).toBe('litecoin');
    expect(chainDisplayName('')).toBe('');
  });

  it('does not mistake an inherited property for a chain', () => {
    // A lookup written with `in` or a bare index would find these on the
    // prototype and render "undefined" or a function's source.
    expect(chainDisplayName('toString')).toBe('toString');
    expect(chainDisplayName('constructor')).toBe('constructor');
    expect(chainDisplayName('__proto__')).toBe('__proto__');
  });
});

describe('addressHint: empty', () => {
  it.each(CHAIN_KEYS)('shows the %s format hint when nothing is typed', (chainKey) => {
    expect(addressHint(chainKey, '')).toEqual({ message: CHAINS[chainKey].formatHint });
  });

  it.each(CHAIN_KEYS)('treats whitespace alone as empty on %s', (chainKey) => {
    expect(addressHint(chainKey, '   \t')).toEqual({ message: CHAINS[chainKey].formatHint });
  });

  it('describes the Bitcoin prefixes, testnet included', () => {
    const hint = CHAINS.bitcoin.formatHint;

    for (const prefix of ['bc1', '1', '3', 'tb1', 'm', 'n', '2']) {
      expect(hint).toContain(prefix);
    }
    expect(hint).toMatch(/testnet/i);
  });

  it('describes the Kaspa prefixes and says the prefix is part of the address', () => {
    const hint = CHAINS.kaspa.formatHint;

    expect(hint).toContain('kaspa:');
    expect(hint).toContain('kaspatest:');
    expect(hint).toMatch(/prefix is part of the address/i);
  });
});

describe('addressHint: an extended key', () => {
  const PREFIXES = ['xpub', 'ypub', 'zpub', 'tpub', 'upub', 'vpub'];

  it.each(PREFIXES.flatMap((prefix) => CHAIN_KEYS.map((chainKey) => [prefix, chainKey])))(
    'says %s is not a single address on %s',
    (prefix, chainKey) => {
      const hint = addressHint(
        chainKey as 'bitcoin' | 'kaspa',
        `${prefix}6CUGRUonZSQ4TWtTMmzXdrXDtyPWKi`,
      );

      expect(hint?.message).toMatch(/only single addresses are supported/i);
      // No chain can take an extended key, so there is nothing to switch to.
      expect(hint?.switchTo).toBeUndefined();
    },
  );

  it('recognises the prefix before anything else is typed', () => {
    expect(addressHint('bitcoin', 'tpub')?.message).toMatch(/only single addresses/i);
  });

  it('ignores surrounding whitespace', () => {
    expect(addressHint('bitcoin', '  vpub5Y')?.message).toMatch(/only single addresses/i);
  });

  it('is checked before the Kaspa-looks-like-Bitcoin rule', () => {
    // A `tpub` on Kaspa is an extended key first; calling it "Bitcoin" and
    // offering a switch would lead straight into a second refusal.
    expect(addressHint('kaspa', 'tpubD6NzVbkrYhZ4')?.switchTo).toBeUndefined();
  });
});

describe('addressHint: a Kaspa prefix while Bitcoin is selected', () => {
  it.each([ADDRESSES.kasPrimary, ADDRESSES.kasSecondary, 'kaspatest:', 'kaspa:', 'kaspa:qq'])(
    'offers to switch to Kaspa for %s',
    (typed) => {
      const hint = addressHint('bitcoin', typed);

      expect(hint?.message).toMatch(/looks like a kaspa address/i);
      expect(hint?.switchTo).toBe('kaspa');
    },
  );

  it('recognises an upper-case prefix', () => {
    expect(addressHint('bitcoin', 'KASPATEST:QQ')?.switchTo).toBe('kaspa');
  });

  it('does not offer the switch once Kaspa is already selected', () => {
    expect(addressHint('kaspa', ADDRESSES.kasPrimary)).toBeUndefined();
  });
});

describe('addressHint: a Bitcoin shape while Kaspa is selected', () => {
  it.each([
    ADDRESSES.btcSegwit,
    ADDRESSES.btcLegacy,
    ADDRESSES.btcScript,
    ADDRESSES.btcRegtest,
    'bcrt1q',
    'BCRT1Q',
    'nX',
    'bc1q',
    'BC1Q',
    '1A',
    '3J',
  ])('offers to switch to Bitcoin for %s', (typed) => {
    const hint = addressHint('kaspa', typed);

    expect(hint?.message).toMatch(/looks like a bitcoin address/i);
    expect(hint?.switchTo).toBe('bitcoin');
  });

  it('does not offer the switch once Bitcoin is already selected', () => {
    expect(addressHint('bitcoin', ADDRESSES.btcSegwit)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcLegacy)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcScript)).toBeUndefined();
    expect(addressHint('bitcoin', ADDRESSES.btcRegtest)).toBeUndefined();
  });
});

describe('addressHint: Kaspa selected, no prefix', () => {
  it('says a Kaspa address includes its network prefix', () => {
    // A Kaspa payload pasted without `kaspatest:` - the prefix is part of the
    // checksum, so the server will refuse it, and this says why in advance.
    const payloadOnly = ADDRESSES.kasSecondary.slice('kaspatest:'.length);

    const hint = addressHint('kaspa', payloadOnly);

    expect(hint?.message).toMatch(/include their network prefix/i);
    expect(hint?.switchTo).toBeUndefined();
  });

  it('says so while the prefix itself is still being typed', () => {
    expect(addressHint('kaspa', 'kaspates')?.message).toMatch(/network prefix/i);
  });

  it('has nothing to add once a prefix is there', () => {
    expect(addressHint('kaspa', 'kaspatest:')).toBeUndefined();
    expect(addressHint('kaspa', 'kaspatest:qqnapngv')).toBeUndefined();
  });
});

describe('addressHint: never a verdict', () => {
  it('has nothing to say about a string it cannot place on Bitcoin', () => {
    // No checksum is checked here, by design: the server's is the only
    // verdict, and a second implementation would eventually disagree with it.
    expect(addressHint('bitcoin', 'not an address at all')).toBeUndefined();
    expect(addressHint('bitcoin', 'tb1qqqqqqq')).toBeUndefined();
  });

  it('returns a hint without echoing the address back', () => {
    // The hint is rendered on the page and could end up in a screenshot or an
    // error report; it describes the shape, it does not repeat the value.
    for (const [chainKey, typed] of [
      ['bitcoin', ADDRESSES.kasPrimary],
      ['kaspa', ADDRESSES.btcSegwit],
    ] as const) {
      expect(addressHint(chainKey, typed)?.message).not.toContain(typed);
    }
  });
});
