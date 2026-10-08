import { describe, expect, it } from 'vitest';

import { currencyLabel, DISPLAY_CURRENCY, VALUATION_CURRENCY } from '@/lib/currency';

describe('currencyLabel', () => {
  it('labels the valuation currency as the stablecoin the portfolio is valued in', () => {
    expect(VALUATION_CURRENCY).toBe('USD');
    expect(DISPLAY_CURRENCY).toBe('USDT');
    expect(currencyLabel('USD')).toBe('USDT');
  });

  it('leaves any other currency as the backend names it', () => {
    // A figure in another currency is labelled for what it is, never relabelled as USDT.
    expect(currencyLabel('EUR')).toBe('EUR');
    expect(currencyLabel('USDT')).toBe('USDT');
  });
});
