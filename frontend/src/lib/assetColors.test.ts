import { describe, expect, it } from 'vitest';

import { assetColors } from '@/lib/assetColors';

describe('assetColors', () => {
  it('gives each tracked asset its own slot', () => {
    const colors = assetColors(['BTC', 'KAS']);

    expect(colors.get('BTC')).toBe('var(--series-orange)');
    expect(colors.get('KAS')).toBe('var(--series-aqua)');
  });

  it('keeps an asset its colour whatever its rank', () => {
    // KAS overtaking BTC reorders the holdings; it must not repaint them.
    expect(assetColors(['KAS', 'BTC'])).toEqual(assetColors(['BTC', 'KAS']));
  });

  it('hands the spare slot out alphabetically, and grey after it', () => {
    const colors = assetColors(['ZZZ', 'BTC', 'ETH', 'AAA']);

    expect(colors.get('AAA')).toBe('var(--series-blue)');
    expect(colors.get('ETH')).toBe('var(--series-other)');
    expect(colors.get('ZZZ')).toBe('var(--series-other)');
    expect(colors.get('BTC')).toBe('var(--series-orange)');
  });

  it('never gives two assets the same hue while slots last', () => {
    const colors = assetColors(['BTC', 'KAS', 'ETH']);

    expect(new Set(colors.values()).size).toBe(3);
  });

  it('counts an asset listed twice once', () => {
    const colors = assetColors(['ETH', 'ETH', 'SOL']);

    expect(colors.get('ETH')).toBe('var(--series-blue)');
    expect(colors.get('SOL')).toBe('var(--series-other)');
    expect(colors.size).toBe(2);
  });
});
