import { describe, expect, it } from 'vitest';

import { truncateAddress } from '@/lib/addresses';
import { ADDRESSES } from '@/test/fixtures';

/**
 * The truncation rule from the spec:
 *
 * - with a `:`, keep the prefix, the `:` and 6 characters, `…`, the last 6;
 * - without one, keep the first 8, `…`, the last 6;
 * - an address short enough not to need it stays whole.
 *
 * Expected strings are written out by hand, not sliced from the input with
 * the same arithmetic the implementation uses.
 */
describe('truncateAddress', () => {
  it('keeps a prefixed address its prefix, six characters and the last six', () => {
    expect(truncateAddress(ADDRESSES.kasPrimary)).toBe('kaspatest:qxaqrl…gdmpks');
    expect(truncateAddress(ADDRESSES.kasSecondary)).toBe('kaspatest:qqnapn…9evrfz');
  });

  it('keeps the whole prefix, however long it is', () => {
    // The prefix says which network the address is on. Cutting into it would
    // make a testnet address and a mainnet one look alike on screen.
    expect(truncateAddress('averyveryverylongprefix:abcdefghijklmnopqrstuvwxyz')).toBe(
      'averyveryverylongprefix:abcdef…uvwxyz',
    );
  });

  it('keeps an unprefixed address its first eight and last six characters', () => {
    expect(truncateAddress(ADDRESSES.btcSegwit)).toBe('tb1qw508…xpjzsx');
    expect(truncateAddress(ADDRESSES.btcLegacy)).toBe('mwgS2HRb…fFBmGq');
    expect(truncateAddress(ADDRESSES.btcScript)).toBe('2MwBVrJQ…pe4NqU');
    expect(truncateAddress(ADDRESSES.btcRegtest)).toBe('bcrt1qda…7tmnu9');
  });

  it('uses a single ellipsis character, not three dots', () => {
    const short = truncateAddress(ADDRESSES.btcSegwit);

    expect(short).toContain('…');
    expect(short).not.toContain('...');
    expect(short.match(/…/g)).toHaveLength(1);
  });

  it('leaves a short unprefixed address whole', () => {
    // 14 characters: the first 8 plus the last 6 already cover all of it.
    expect(truncateAddress('abcdefghijklmn')).toBe('abcdefghijklmn');
    expect(truncateAddress('abc')).toBe('abc');
    expect(truncateAddress('')).toBe('');
  });

  it('shortens an unprefixed address one character past the threshold', () => {
    expect(truncateAddress('abcdefghijklmno')).toBe('abcdefgh…jklmno');
  });

  it('leaves a short prefixed address whole', () => {
    // "kaspatest:" plus 12 characters is exactly prefix + 6 + last 6.
    expect(truncateAddress('kaspatest:qqqqqqzzzzzz')).toBe('kaspatest:qqqqqqzzzzzz');
    expect(truncateAddress('kaspatest:')).toBe('kaspatest:');
  });

  it('shortens a prefixed address one character past the threshold', () => {
    expect(truncateAddress('kaspatest:qqqqqqxzzzzzz')).toBe('kaspatest:qqqqqq…zzzzzz');
  });

  it('never returns something longer than the address', () => {
    for (const address of Object.values(ADDRESSES)) {
      expect(truncateAddress(address).length).toBeLessThanOrEqual(address.length);
    }
  });

  it('keeps the characters it shows in place', () => {
    // Whatever is shown must be a real prefix and a real suffix of the
    // address, so the owner can match it against a block explorer.
    for (const address of Object.values(ADDRESSES)) {
      const [head = '', tail = ''] = truncateAddress(address).split('…');

      expect(address.startsWith(head)).toBe(true);
      expect(address.endsWith(tail)).toBe(true);
      expect(tail).toHaveLength(6);
    }
  });
});
