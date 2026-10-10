import { describe, expect, it } from 'vitest';

import {
  describeDifference,
  describeProfit,
  formatQuantity,
  formatSignedPercent,
  formatUsdt,
  investedPoints,
  investmentUnavailableWords,
} from '@/lib/investment';
import { money } from '@/lib/money';

describe('the invested words', () => {
  it('says why a profit is missing, per reason, and never as a zero', () => {
    expect([
      investmentUnavailableWords('unvalued_trades'),
      investmentUnavailableWords('value_unknown'),
      investmentUnavailableWords('nothing_invested'),
    ]).toEqual([
      'Not available: a trade was not priced in USDT, USDC or DAI.',
      'Not available: the value now is unknown.',
      'No percentage: nothing is invested.',
    ]);
  });

  it('writes an amount in USDT at two places, and a quantity at up to eight', () => {
    expect(formatUsdt(money('13007.000000000000000000'))).toBe('13,007.00 USDT');
    expect(formatQuantity(money('0.123450000000000000'), 'BTC')).toBe('0.12345 BTC');
    expect(formatQuantity(money('123456.780000000000000000'), 'KAS')).toBe('123,456.78 KAS');
    expect(formatQuantity(money('0.000000001'), 'BTC')).toBe('< 0.00000001 BTC');
  });

  it('gives a profit a sign and a word, so it reads the same without colour', () => {
    expect(describeProfit(money('10993.000000000000000000'))).toEqual({
      direction: 'up',
      text: '+10,993.00 USDT gain',
    });
    expect(describeProfit(money('-100'))).toEqual({
      direction: 'down',
      text: '-100.00 USDT loss',
    });
    expect(describeProfit(money('0'))).toEqual({ direction: 'flat', text: '0.00 USDT, even' });
  });

  it('signs a percentage', () => {
    expect(formatSignedPercent(money('84.5160'))).toBe('+84.52%');
    expect(formatSignedPercent(money('-14.2857'))).toBe('-14.29%');
    expect(formatSignedPercent(money('0'))).toBe('0.00%');
  });

  it('says which way the wallets and the operations disagree, or that they match', () => {
    expect(describeDifference(money('0'), 'BTC')).toBe('Matches the operations.');
    expect(describeDifference(money('0.5'), 'BTC')).toBe(
      '+0.5 BTC: the wallets hold more than the operations explain.',
    );
    expect(describeDifference(money('-2500.5'), 'KAS')).toBe(
      '-2,500.5 KAS: the wallets hold less than the operations explain.',
    );
  });
});

describe('investedPoints', () => {
  const steps = [
    { day: '2026-05-02', invested: '100' },
    { day: '2026-05-04', invested: null },
    { day: '2026-05-05', invested: '250' },
  ];

  it('carries the last step at or before each day, with a gap before the first', () => {
    expect(
      investedPoints(
        ['2026-05-01', '2026-05-02', '2026-05-03', '2026-05-04', '2026-05-05', '2026-05-09'],
        steps,
      ),
    ).toEqual([
      { day: '2026-05-01', value: null },
      { day: '2026-05-02', value: '100' },
      { day: '2026-05-03', value: '100' },
      { day: '2026-05-04', value: null },
      { day: '2026-05-05', value: '250' },
      { day: '2026-05-09', value: '250' },
    ]);
  });

  it('draws nothing when nothing was ever invested', () => {
    expect(investedPoints(['2026-05-01'], [])).toEqual([{ day: '2026-05-01', value: null }]);
  });
});
