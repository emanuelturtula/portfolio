import { describe, expect, it } from 'vitest';

import { formatMonth, listExchanges } from '@/lib/exports';

describe('formatMonth', () => {
  it.each([
    ['2026-09', 'September 2026'],
    ['2026-12', 'December 2026'],
    ['2027-01', 'January 2027'],
  ])('reads %s as %s', (month, expected) => {
    expect(formatMonth(month)).toBe(expected);
  });

  it('leaves anything else as it came', () => {
    expect(formatMonth('2026-13')).toBe('2026-13');
    expect(formatMonth('soon')).toBe('soon');
  });
});

describe('listExchanges', () => {
  it('joins the names as a sentence does', () => {
    expect(listExchanges(['Binance', 'Bitget', 'BingX', 'Nexo'])).toBe(
      'Binance, Bitget, BingX, and Nexo',
    );
    expect(listExchanges(['Bitget'])).toBe('Bitget');
  });
});
