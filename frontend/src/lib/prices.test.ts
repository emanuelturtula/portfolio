import { describe, expect, it } from 'vitest';

import { PRICE_UNAVAILABLE_MESSAGES } from '@/lib/prices';
import type { PriceUnavailable } from '@/test/fixtures';

/**
 * Every `PriceUnavailable` reason, written out by hand. A `Record` so that a
 * reason added on the backend fails `tsc` here until this list names it.
 */
const ALL_REASONS_RECORD: Record<PriceUnavailable, true> = {
  never_fetched: true,
  every_source_failed: true,
  unsupported_pair: true,
  no_source_configured: true,
};
const ALL_REASONS = Object.keys(ALL_REASONS_RECORD) as PriceUnavailable[];

describe('PRICE_UNAVAILABLE_MESSAGES', () => {
  it('has a sentence for every reason and nothing else', () => {
    expect(Object.keys(PRICE_UNAVAILABLE_MESSAGES).sort()).toEqual([...ALL_REASONS].sort());
  });

  it.each(ALL_REASONS)('says something for %s', (reason) => {
    expect(PRICE_UNAVAILABLE_MESSAGES[reason].trim().length).toBeGreaterThan(0);
  });

  it('gives every reason a different sentence', () => {
    // The four reasons point at four different things to go and look at: the
    // refresh, the vendors, the request, the configuration. One shared
    // "price unavailable" would collapse exactly what the backend kept apart.
    const sentences = ALL_REASONS.map((reason) => PRICE_UNAVAILABLE_MESSAGES[reason]);

    expect(new Set(sentences).size).toBe(ALL_REASONS.length);
  });

  it('never phrases a missing price as a zero', () => {
    for (const reason of ALL_REASONS) {
      expect(PRICE_UNAVAILABLE_MESSAGES[reason]).not.toMatch(/\b0\b|zero/i);
    }
  });
});
