import { describe, expect, it } from 'vitest';

import type { MissingKind } from '@/api/portfolio';
import { describeMissing, valueIsPartial } from '@/lib/portfolio';
import { missing } from '@/test/summaryFixtures';

const KINDS: readonly MissingKind[] = ['wallet_unread', 'wallet_stale', 'unpriced', 'stale_price'];

describe('valueIsPartial', () => {
  it.each(KINDS)('%s leaves the value partial', (kind) => {
    expect(valueIsPartial([missing(kind, 'x')])).toBe(true);
  });

  it('nothing missing is nothing partial', () => {
    expect(valueIsPartial([])).toBe(false);
  });
});

describe('describeMissing', () => {
  it.each([
    [missing('wallet_unread', 'kaspa'), 'Kaspa wallet not read yet'],
    [missing('wallet_stale', 'bitcoin'), 'Bitcoin balance out of date'],
    [missing('unpriced', 'KAS'), 'no price for KAS'],
    [missing('stale_price', 'BTC'), 'BTC price out of date'],
  ])('%o reads "%s"', (piece, expected) => {
    expect(describeMissing(piece)).toBe(expected);
  });

  it('names a chain it does not know by its key rather than failing', () => {
    expect(describeMissing(missing('wallet_unread', 'litecoin'))).toBe(
      'litecoin wallet not read yet',
    );
  });
});
