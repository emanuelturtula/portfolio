import { describe, expect, it } from 'vitest';

import {
  axisDayFormat,
  formatAxisValue,
  formatDay,
  GAPS_NOTE,
  PORTFOLIO_EMPTY_WORDS,
  portfolioSeries,
  RANGE_LABELS,
  RANGE_PHRASES,
  summarize,
  toChartPoints,
  TOTAL_COLOR,
  TOTAL_SERIES,
  WALLET_EMPTY_WORDS,
} from '@/lib/history';
import { inTimeZone } from '@/test/timeZone';

const A = '29000.000000000000000000';
const B = '29500.000000000000000000';
const C = '30770.000000000000000000';

describe('the history words', () => {
  it('has a label and a phrase for exactly the ranges the API accepts', () => {
    expect(Object.keys(RANGE_LABELS)).toEqual(['30d', '90d', '1y', 'all']);
    expect(Object.values(RANGE_LABELS)).toEqual(['30D', '90D', '1Y', 'All']);
    expect(Object.keys(RANGE_PHRASES)).toEqual(['30d', '90d', '1y', 'all']);
  });

  it('never words the empty chart like the gaps in a chart, or one subject like the other', () => {
    const sentences = [GAPS_NOTE, PORTFOLIO_EMPTY_WORDS, WALLET_EMPTY_WORDS];

    expect(new Set(sentences).size).toBe(sentences.length);
    for (const sentence of sentences) {
      expect(sentence).not.toMatch(/\b0\b|zero|error|fail/i);
    }
  });
});

describe('formatDay', () => {
  it('writes a served day as that day, whatever the browser zone', () => {
    // West of Greenwich, midnight UTC is still the evening before.
    inTimeZone('America/Los_Angeles');

    expect(formatDay('2026-09-24')).toBe('Sep 24, 2026');
    expect(formatDay('2026-01-01')).toBe('Jan 1, 2026');
  });
});

describe('axisDayFormat', () => {
  it('names days over a few months, and months over a longer span', () => {
    inTimeZone('Pacific/Auckland');

    expect(axisDayFormat(90)('2026-09-24')).toBe('Sep 24');
    expect(axisDayFormat(120)('2026-09-24')).toBe('Sep 24');
    expect(axisDayFormat(121)('2026-09-01')).toBe('Sep 2026');
    expect(axisDayFormat(365)('2026-01-31')).toBe('Jan 2026');
  });
});

describe('formatAxisValue', () => {
  it('writes a gridline compactly, so it reads as a scale and not as an amount', () => {
    expect(formatAxisValue(0)).toBe('0');
    expect(formatAxisValue(30_000)).toBe('30K');
    expect(formatAxisValue(1_250_000)).toBe('1.3M');
  });
});

describe('toChartPoints', () => {
  it('keeps a day with no value null, never zero, and keeps the exact strings', () => {
    const points = toChartPoints([
      { day: '2026-09-22', value: null },
      { day: '2026-09-23', value: A },
    ]);

    expect(points).toEqual([
      { day: '2026-09-22', value: null, quantity: null, y: null, marked: false },
      { day: '2026-09-23', value: A, quantity: null, y: 29000, marked: true },
    ]);
  });

  it("carries a wallet's quantity, and none before its first reading", () => {
    const points = toChartPoints([
      { day: '2026-09-22', quantity: null, value: null },
      { day: '2026-09-23', quantity: '0.4995', value: null },
      { day: '2026-09-24', quantity: '0.4995', value: A },
    ]);

    expect(points.map((point) => point.quantity)).toEqual([null, '0.4995', '0.4995']);
  });

  it('marks the last valued day and every valued day between two gaps', () => {
    const points = toChartPoints([
      { day: '2026-09-19', value: A },
      { day: '2026-09-20', value: null },
      { day: '2026-09-21', value: B },
      { day: '2026-09-22', value: C },
      { day: '2026-09-23', value: null },
      { day: '2026-09-24', value: null },
    ]);

    // The first stands alone; the line of two is a line; the last of it ends the chart, and
    // the two days after it have nothing to draw.
    expect(points.map((point) => point.marked)).toEqual([true, false, false, true, false, false]);
  });

  it('marks nothing when nothing has a value', () => {
    expect(toChartPoints([{ day: '2026-09-24', value: null }])).toEqual([
      { day: '2026-09-24', value: null, quantity: null, y: null, marked: false },
    ]);
  });
});

describe('summarize', () => {
  it('names the first and last valued days and counts the gaps', () => {
    const summary = summarize(
      toChartPoints([
        { day: '2026-09-21', value: null },
        { day: '2026-09-22', value: A },
        { day: '2026-09-23', value: null },
        { day: '2026-09-24', value: C },
      ]),
    );

    expect(summary).toEqual({
      first: { day: '2026-09-22', value: A },
      last: { day: '2026-09-24', value: C },
      gaps: 2,
    });
  });

  it('is one day at both ends when only one day has a value', () => {
    const summary = summarize(toChartPoints([{ day: '2026-09-24', value: A }]));

    expect(summary).toEqual({
      first: { day: '2026-09-24', value: A },
      last: { day: '2026-09-24', value: A },
      gaps: 0,
    });
  });

  it('is nothing at all when no day has a value, or there is no day', () => {
    expect(summarize(toChartPoints([{ day: '2026-09-24', value: null }]))).toBeUndefined();
    expect(summarize([])).toBeUndefined();
  });
});

describe('portfolioSeries', () => {
  const data = {
    assets: ['BTC', 'KAS'],
    points: [
      { day: '2026-09-23', value: A, assets: { BTC: B, KAS: null } },
      { day: '2026-09-24', value: C, assets: { BTC: C } },
    ],
  };
  const colors = new Map([['BTC', 'var(--series-orange)']]);

  it('offers the total first, then each asset, keeping only those chosen', () => {
    const lines = portfolioSeries(data, ['KAS', 'total', 'BTC'], colors);

    expect(lines.map((line) => [line.key, line.label, line.color])).toEqual([
      [TOTAL_SERIES, 'Total', TOTAL_COLOR],
      ['BTC', 'BTC', 'var(--series-orange)'],
      ['KAS', 'KAS', 'var(--series-other)'],
    ]);
    expect(lines[0]?.points).toEqual([
      { day: '2026-09-23', value: A },
      { day: '2026-09-24', value: C },
    ]);
    // A day an asset is not served on is a gap, never a zero.
    expect(lines[2]?.points).toEqual([
      { day: '2026-09-23', value: null },
      { day: '2026-09-24', value: null },
    ]);
  });

  it('draws the total alone when that is all that is chosen', () => {
    expect(portfolioSeries(data, [TOTAL_SERIES], colors).map((line) => line.key)).toEqual([
      TOTAL_SERIES,
    ]);
  });
});
