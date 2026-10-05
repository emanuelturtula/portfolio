import { describe, expect, it } from 'vitest';

import {
  dayFromInput,
  dayRangeToInstants,
  displayTimeZone,
  EXCHANGE_KEYS,
  FILLS_PAGE_SIZE,
  fillsQuery,
  formatDayLabel,
  hasActiveFilters,
  isInvertedRange,
  NO_FILTERS,
  readFillFilters,
  toggleExchange,
  writeFillFilters,
  type FillFilters,
} from '@/lib/fillFilters';
import { ALL_EXCHANGE_KEYS } from '@/test/exchangeFixtures';
import { inTimeZone } from '@/test/timeZone';

/**
 * Spec 024, "Transactions": the filters live in the URL, a picked day is the browser's local
 * day, the end day is inclusive in the UI and exclusive at the API, and "next local midnight"
 * is calendar arithmetic, never `midnight + 24 h`.
 *
 * Every instant below is written out by hand from the zone's rules, not computed with
 * `Date`: the code under test is `Date` arithmetic, and an expectation built the same way
 * would agree with any bug in it.
 */

const HOUR_MS = 3_600_000;

function params(query: string): URLSearchParams {
  return new URLSearchParams(query);
}

describe('the page size', () => {
  it('is 5, fixed', () => {
    expect(FILLS_PAGE_SIZE).toBe(5);
  });
});

describe('the venue choices', () => {
  it('are every ExchangeKey, whether or not the list has loaded', () => {
    expect([...EXCHANGE_KEYS].sort()).toEqual([...ALL_EXCHANGE_KEYS].sort());
  });
});

describe('readFillFilters', () => {
  it('reads nothing as no filters and the first page', () => {
    expect(readFillFilters(params(''))).toEqual({ filters: NO_FILTERS, page: 1 });
  });

  it('reads every parameter', () => {
    expect(
      readFillFilters(
        params('exchange=bitget&exchange=bingx&from=2026-03-01&to=2026-03-31&page=3'),
      ),
    ).toEqual({
      filters: { exchanges: ['bingx', 'bitget'], fromDay: '2026-03-01', toDay: '2026-03-31' },
      page: 3,
    });
  });

  it('drops an unknown venue, counts a repeated one once, and orders them one way', () => {
    // Two URLs that mean the same thing must share one query key.
    const one = readFillFilters(params('exchange=bitget&exchange=kraken&exchange=bitget'));
    const other = readFillFilters(params('exchange=bitget'));

    expect(one.filters.exchanges).toEqual(['bitget']);
    expect(one).toEqual(other);
    expect(readFillFilters(params('exchange=bitget&exchange=bingx')).filters.exchanges).toEqual(
      readFillFilters(params('exchange=bingx&exchange=bitget')).filters.exchanges,
    );
    // A venue's key is exact: case and padding are not forgiven.
    expect(readFillFilters(params('exchange=Bitget&exchange=%20bingx')).filters.exchanges).toEqual(
      [],
    );
  });

  it.each([
    ['2026-02-30', 'a day February does not have'],
    ['2026-13-01', 'a month that does not exist'],
    ['2026-3-1', 'a day not written YYYY-MM-DD'],
    ['2026-03-01T00:00:00Z', 'an instant rather than a day'],
    ['20260301', 'a digit string'],
    ['', 'nothing'],
    ['yesterday', 'a word'],
  ])('drops %j, %s', (value) => {
    const read = readFillFilters(
      params(`from=${encodeURIComponent(value)}&to=${encodeURIComponent(value)}`),
    );

    expect(read.filters.fromDay).toBeNull();
    expect(read.filters.toDay).toBeNull();
  });

  it('keeps a leap day in a leap year', () => {
    expect(readFillFilters(params('from=2028-02-29')).filters.fromDay).toBe('2028-02-29');
  });

  it.each([
    ['1969-12-31', 'the day before the first day the pickers offer'],
    ['0001-01-01', 'the first day of the calendar'],
    ['0099-06-15', 'a two-digit year, which Date reads as 19xx'],
    ['9999-12-31', 'the last day, whose next midnight no instant can hold'],
  ])('drops %j, %s (R5, N4)', (value) => {
    const read = readFillFilters(params(`from=${value}&to=${value}`));

    expect(read.filters.fromDay).toBeNull();
    expect(read.filters.toDay).toBeNull();
  });

  it.each(['1970-01-01', '9999-12-30'])('keeps %j, a bound the pickers offer (R5, N4)', (value) => {
    const read = readFillFilters(params(`from=${value}&to=${value}`));

    expect(read.filters.fromDay).toBe(value);
    expect(read.filters.toDay).toBe(value);
  });

  it.each(['0', '-1', '1.5', '1e3', 'abc', '', '02', '1000000'])(
    'reads page %j as the first page',
    (value) => {
      expect(readFillFilters(params(`page=${value}`)).page).toBe(1);
    },
  );

  it.each([
    ['1', 1],
    ['2', 2],
    ['17', 17],
    ['999999', 999_999],
  ])('reads page %j as %i', (value, expected) => {
    expect(readFillFilters(params(`page=${value}`)).page).toBe(expected);
  });

  it('keeps an inverted range, so the form can show what was typed and refuse it', () => {
    expect(readFillFilters(params('from=2026-03-31&to=2026-03-01')).filters).toEqual({
      exchanges: [],
      fromDay: '2026-03-31',
      toDay: '2026-03-01',
    });
  });
});

describe('writeFillFilters', () => {
  it('writes nothing for no filters on the first page', () => {
    expect(writeFillFilters(NO_FILTERS, 1).toString()).toBe('');
  });

  it('writes every filter, and the page only past the first', () => {
    const filters: FillFilters = {
      exchanges: ['bingx', 'bitget'],
      fromDay: '2026-03-01',
      toDay: '2026-03-31',
    };

    expect(writeFillFilters(filters, 1).toString()).toBe(
      'exchange=bingx&exchange=bitget&from=2026-03-01&to=2026-03-31',
    );
    expect(writeFillFilters(filters, 4).toString()).toBe(
      'exchange=bingx&exchange=bitget&from=2026-03-01&to=2026-03-31&page=4',
    );
    expect(writeFillFilters({ ...NO_FILTERS, toDay: '2026-03-31' }, 2).toString()).toBe(
      'to=2026-03-31&page=2',
    );
  });

  it('round-trips through the URL', () => {
    const cases: { filters: FillFilters; page: number }[] = [
      { filters: NO_FILTERS, page: 1 },
      { filters: { exchanges: ['bitget'], fromDay: null, toDay: null }, page: 2 },
      { filters: { exchanges: [], fromDay: '2026-03-29', toDay: '2026-03-29' }, page: 1 },
      {
        filters: { exchanges: ['bingx', 'bitget'], fromDay: '2026-01-01', toDay: '2026-12-31' },
        page: 12,
      },
    ];

    for (const { filters, page } of cases) {
      expect(readFillFilters(writeFillFilters(filters, page))).toEqual({ filters, page });
    }
  });
});

describe('the filter helpers', () => {
  it('a filter is active when any of the three is set', () => {
    expect(hasActiveFilters(NO_FILTERS)).toBe(false);
    expect(hasActiveFilters({ ...NO_FILTERS, exchanges: ['bingx'] })).toBe(true);
    expect(hasActiveFilters({ ...NO_FILTERS, fromDay: '2026-03-01' })).toBe(true);
    expect(hasActiveFilters({ ...NO_FILTERS, toDay: '2026-03-01' })).toBe(true);
  });

  it('a range is inverted only when the end day is before the start day', () => {
    expect(isInvertedRange({ ...NO_FILTERS, fromDay: '2026-03-02', toDay: '2026-03-01' })).toBe(
      true,
    );
    expect(isInvertedRange({ ...NO_FILTERS, fromDay: '2026-12-31', toDay: '2027-01-01' })).toBe(
      false,
    );
    // One day on both ends is a one-day range.
    expect(isInvertedRange({ ...NO_FILTERS, fromDay: '2026-03-01', toDay: '2026-03-01' })).toBe(
      false,
    );
    expect(isInvertedRange({ ...NO_FILTERS, fromDay: '2026-03-02' })).toBe(false);
    expect(isInvertedRange({ ...NO_FILTERS, toDay: '2026-03-01' })).toBe(false);
  });

  it('toggles a venue in and out', () => {
    const one = toggleExchange(NO_FILTERS, 'bitget');
    expect(one.exchanges).toEqual(['bitget']);
    expect(toggleExchange(one, 'bingx').exchanges).toEqual(['bitget', 'bingx']);
    expect(toggleExchange(one, 'bitget').exchanges).toEqual([]);
  });

  it('reads an emptied date input as no day', () => {
    expect(dayFromInput('')).toBeNull();
    expect(dayFromInput('2026-03-01')).toBe('2026-03-01');
  });

  it('names a day the way the owner reads it', () => {
    inTimeZone('Pacific/Kiritimati');
    // UTC+14: a formatter that read the day as UTC midnight would still say Mar 1 here, but
    // one west of UTC would not; the next test moves the other way.
    expect(formatDayLabel('2026-03-01')).toBe('Mar 1, 2026');
  });

  it('names the same day west of UTC', () => {
    inTimeZone('Pacific/Pago_Pago');
    // UTC-11. `new Date('2026-03-01')` is UTC midnight, which is 28 Feb here.
    expect(formatDayLabel('2026-03-01')).toBe('Mar 1, 2026');
  });
});

describe('displayTimeZone', () => {
  it('is the zone the browser resolves', () => {
    inTimeZone('Europe/Madrid');
    expect(displayTimeZone()).toBe('Europe/Madrid');
  });

  it('follows the zone, rather than naming one', () => {
    inTimeZone('America/New_York');
    expect(displayTimeZone()).toBe('America/New_York');
  });
});

describe('dayRangeToInstants', () => {
  it('leaves an unset day unset', () => {
    expect(dayRangeToInstants(null, null)).toEqual({ from: null, to: null });
  });

  it('is UTC midnight and the next UTC midnight in UTC', () => {
    inTimeZone('UTC');
    expect(dayRangeToInstants('2026-03-01', '2026-03-31')).toEqual({
      from: '2026-03-01T00:00:00.000Z',
      to: '2026-04-01T00:00:00.000Z',
    });
  });

  it('is the local midnight, not the UTC one', () => {
    inTimeZone('Europe/Madrid');
    // Winter: CET, UTC+1.
    expect(dayRangeToInstants('2026-03-01', '2026-03-01')).toEqual({
      from: '2026-02-28T23:00:00.000Z',
      to: '2026-03-01T23:00:00.000Z',
    });
  });

  it('ends a day on which the clocks go forward after 23 hours, not 24', () => {
    inTimeZone('Europe/Madrid');
    // 29 March 2026: 02:00 CET becomes 03:00 CEST. The day starts at UTC+1 and the next
    // one at UTC+2, so it is 23 hours long. `+24 h` would reach 23:00Z, an hour into 30 March.
    const range = dayRangeToInstants('2026-03-29', '2026-03-29');

    expect(range).toEqual({ from: '2026-03-28T23:00:00.000Z', to: '2026-03-29T22:00:00.000Z' });
    expect(Date.parse(range.to ?? '') - Date.parse(range.from ?? '')).toBe(23 * HOUR_MS);
  });

  it('ends a day on which the clocks go back after 25 hours, not 24', () => {
    inTimeZone('Europe/Madrid');
    // 25 October 2026: 03:00 CEST becomes 02:00 CET. `+24 h` would stop at 22:00Z and drop
    // the last hour of the day.
    const range = dayRangeToInstants('2026-10-25', '2026-10-25');

    expect(range).toEqual({ from: '2026-10-24T22:00:00.000Z', to: '2026-10-25T23:00:00.000Z' });
    expect(Date.parse(range.to ?? '') - Date.parse(range.from ?? '')).toBe(25 * HOUR_MS);
  });

  it('crosses a DST change inside a longer range', () => {
    inTimeZone('America/New_York');
    // 8 March 2026, EST to EDT. From 1 March (UTC-5) to 31 March inclusive (1 April, UTC-4).
    expect(dayRangeToInstants('2026-03-01', '2026-03-31')).toEqual({
      from: '2026-03-01T05:00:00.000Z',
      to: '2026-04-01T04:00:00.000Z',
    });
  });

  it('rolls the end day over a month, a year and a leap day', () => {
    inTimeZone('UTC');
    expect(dayRangeToInstants(null, '2026-02-28').to).toBe('2026-03-01T00:00:00.000Z');
    expect(dayRangeToInstants(null, '2028-02-28').to).toBe('2028-02-29T00:00:00.000Z');
    expect(dayRangeToInstants(null, '2028-02-29').to).toBe('2028-03-01T00:00:00.000Z');
    expect(dayRangeToInstants(null, '2026-12-31').to).toBe('2027-01-01T00:00:00.000Z');
  });

  it.each(['Europe/Madrid', 'America/New_York', 'America/Santiago', 'Australia/Lord_Howe'])(
    'tiles every day of 2026 in %s with no gap and no overlap',
    (zone) => {
      // Adjacent days must meet exactly: the end of one is the start of the next, so a fill
      // on the boundary is in exactly one of them. Santiago's clocks change at midnight, so
      // one of its days has no local midnight at all; Lord Howe's move by half an hour.
      inTimeZone(zone);
      const lengths = new Set<number>();
      let day = '2026-01-01';

      for (let index = 0; index < 365; index += 1) {
        const { from, to } = dayRangeToInstants(day, day);
        const next = new Date(Date.parse(`${day}T12:00:00Z`) + 24 * HOUR_MS)
          .toISOString()
          .slice(0, 10);
        expect(dayRangeToInstants(next, null).from, `${day} to ${next}`).toBe(to);
        lengths.add(Date.parse(to ?? '') - Date.parse(from ?? ''));
        day = next;
      }

      // Every day but the two the clocks change on is exactly 24 hours long.
      expect(lengths.has(24 * HOUR_MS)).toBe(true);
      expect(lengths.size).toBe(3);
    },
  );
});

describe('fillsQuery', () => {
  it('asks for the first page of everything with no filter', () => {
    expect(fillsQuery(NO_FILTERS, 1).toString()).toBe('limit=5&offset=0');
  });

  it('sends the venues, the two instants, and the page as an offset', () => {
    inTimeZone('Europe/Madrid');
    const query = fillsQuery(
      { exchanges: ['bingx', 'bitget'], fromDay: '2026-03-29', toDay: '2026-03-29' },
      3,
    );

    expect(query.getAll('exchange')).toEqual(['bingx', 'bitget']);
    expect(query.get('from')).toBe('2026-03-28T23:00:00.000Z');
    expect(query.get('to')).toBe('2026-03-29T22:00:00.000Z');
    expect(query.get('limit')).toBe('5');
    expect(query.get('offset')).toBe('10');
  });

  it('sends only the bound that is set', () => {
    inTimeZone('UTC');
    expect(fillsQuery({ ...NO_FILTERS, toDay: '2026-03-01' }, 1).toString()).toBe(
      'to=2026-03-02T00%3A00%3A00.000Z&limit=5&offset=0',
    );
    expect(fillsQuery({ ...NO_FILTERS, fromDay: '2026-03-01' }, 2).toString()).toBe(
      'from=2026-03-01T00%3A00%3A00.000Z&limit=5&offset=5',
    );
  });
});
