import { describe, expect, it } from 'vitest';

import type { MissingKind } from '@/api/portfolio';
import { describeMissing, investedIsPartial, valueIsPartial } from '@/lib/portfolio';
import { missing } from '@/test/summaryFixtures';

const VALUE_KINDS: readonly MissingKind[] = [
  'wallet_unread',
  'wallet_stale',
  'exchange_unread',
  'exchange_stale',
  'unpriced',
  'stale_price',
];

describe('valueIsPartial and investedIsPartial', () => {
  it.each(VALUE_KINDS)('%s leaves the value partial and the invested figure whole', (kind) => {
    const pieces = [missing(kind, 'x')];

    expect(valueIsPartial(pieces)).toBe(true);
    expect(investedIsPartial(pieces)).toBe(false);
  });

  it('a fill not in cash leaves the invested figure partial and the value whole', () => {
    const pieces = [missing('fill_not_in_cash', 'BTC')];

    expect(valueIsPartial(pieces)).toBe(false);
    expect(investedIsPartial(pieces)).toBe(true);
  });

  it('nothing missing is nothing partial', () => {
    expect(valueIsPartial([])).toBe(false);
    expect(investedIsPartial([])).toBe(false);
  });
});

describe('describeMissing', () => {
  it.each([
    [missing('wallet_unread', 'kaspa'), 'Kaspa wallet not read yet'],
    [missing('wallet_stale', 'bitcoin'), 'Bitcoin balance out of date'],
    [missing('exchange_unread', 'bingx'), 'BingX balances not read yet'],
    [missing('exchange_stale', 'bitget'), 'Bitget balances out of date'],
    [missing('unpriced', 'KAS'), 'no price for KAS'],
    [missing('stale_price', 'BTC'), 'BTC price out of date'],
    [missing('fill_not_in_cash', 'BTC'), 'trades paid in BTC not counted'],
  ])('%o reads "%s"', (piece, expected) => {
    expect(describeMissing(piece)).toBe(expected);
  });

  it('names a venue it does not know by its key rather than failing', () => {
    expect(describeMissing(missing('exchange_unread', 'kraken'))).toBe(
      'kraken balances not read yet',
    );
  });
});
