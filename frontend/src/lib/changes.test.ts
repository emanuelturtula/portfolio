import { describe, expect, it } from 'vitest';

import {
  directionOf,
  formatChange,
  formatPercent,
  PERIOD_LABELS,
  unavailableWords,
} from '@/lib/changes';
import { money } from '@/lib/money';

describe('the change words', () => {
  it('has a heading for exactly the periods the API serves, in its order', () => {
    expect(Object.keys(PERIOD_LABELS)).toEqual(['24h', '7d']);
    expect(Object.values(PERIOD_LABELS)).toEqual(['Last 24 hours', 'Last 7 days']);
  });

  it('says why a change is missing, per reason and period, and never as a zero', () => {
    const sentences = [
      unavailableWords('value_unknown_now', '24h'),
      unavailableWords('no_reading_then', '24h'),
      unavailableWords('no_reading_then', '7d'),
      unavailableWords('no_price_then', '24h'),
      unavailableWords('no_price_then', '7d'),
    ];

    expect(sentences).toEqual([
      'Not available: the total now is incomplete.',
      'Not available: no balance is known for 24 hours ago.',
      'Not available: no balance is known for 7 days ago.',
      'Not available: no price was recorded 24 hours ago.',
      'Not available: no price was recorded 7 days ago.',
    ]);
    for (const sentence of sentences) {
      expect(sentence).not.toMatch(/\b0\b|zero/i);
    }
  });
});

describe('directionOf', () => {
  it.each([
    ['770.000000000000000000', 'up'],
    ['0.000000000000000001', 'up'],
    ['-1230.000000000000000000', 'down'],
    ['0.000000000000000000', 'flat'],
    ['-0', 'flat'],
  ])('reads %s exactly as %s', (value, direction) => {
    expect(directionOf(money(value))).toBe(direction);
  });
});

describe('formatChange and formatPercent', () => {
  it('signs a rise with + and a fall with -, and leaves an exact zero unsigned', () => {
    expect(formatChange(money('770.000000000000000000'))).toBe('+770.00 USDT');
    expect(formatChange(money('-1230.5'))).toBe('-1,230.50 USDT');
    expect(formatChange(money('0'))).toBe('0.00 USDT');
    expect(formatPercent(money('2.5667'))).toBe('+2.57%');
    expect(formatPercent(money('-3.8438'))).toBe('-3.84%');
  });

  it('never shows a tiny rise as +0.00', () => {
    expect(formatChange(money('0.001'))).toBe('< +0.01 USDT');
  });
});
