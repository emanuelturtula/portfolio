import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  formatAbsoluteTime,
  formatDuration,
  formatHistoryStart,
  formatHistoryStartLocal,
  formatRelativeTime,
  parseInstant,
  useNow,
} from '@/lib/time';
import { inTimeZone } from '@/test/timeZone';

const NOW_MS = Date.parse('2026-09-24T12:00:00.000Z');

/** An ISO instant `ms` milliseconds before {@link NOW_MS}. */
function ago(ms: number): string {
  return new Date(NOW_MS - ms).toISOString();
}

const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe('formatRelativeTime', () => {
  it.each([
    [0, 'just now'],
    [44 * SECOND, 'just now'],
    [60 * SECOND, '1 minute ago'],
    [2 * MINUTE, '2 minutes ago'],
    [15 * MINUTE, '15 minutes ago'],
    [18 * MINUTE, '18 minutes ago'],
    [44 * MINUTE, '44 minutes ago'],
    [2 * HOUR, '2 hours ago'],
    [21 * HOUR, '21 hours ago'],
    [3 * DAY, '3 days ago'],
    [400 * DAY, '400 days ago'],
  ])('renders a gap of %i ms as %j', (gap, expected) => {
    expect(formatRelativeTime(ago(gap), NOW_MS)).toBe(expected);
  });

  it('uses the singular for exactly one hour and one day', () => {
    expect(formatRelativeTime(ago(HOUR), NOW_MS)).toBe('1 hour ago');
    expect(formatRelativeTime(ago(DAY), NOW_MS)).toBe('1 day ago');
  });

  it('stops saying "just now" well before a minute has passed', () => {
    // "just now" on a reading three quarters of a minute old is the kind of
    // truthful-looking stale label the ticking clock exists to prevent.
    expect(formatRelativeTime(ago(45 * SECOND), NOW_MS)).not.toBe('just now');
  });

  it('never says a time is in the future', () => {
    // A server clock a few seconds ahead of the browser's is ordinary.
    expect(formatRelativeTime(new Date(NOW_MS + 5 * MINUTE).toISOString(), NOW_MS)).toBe(
      'just now',
    );
  });

  it('reads a microsecond timestamp from the backend', () => {
    expect(formatRelativeTime('2026-09-24T11:45:00.123456Z', NOW_MS)).toBe('15 minutes ago');
  });

  it('reads an instant with an offset as that instant', () => {
    // 10:45 at -01:00 is 11:45 UTC: fifteen minutes before noon UTC.
    expect(formatRelativeTime('2026-09-24T10:45:00-01:00', NOW_MS)).toBe('15 minutes ago');
  });

  it('advances as the clock does', () => {
    const iso = ago(15 * MINUTE);

    expect(formatRelativeTime(iso, NOW_MS)).toBe('15 minutes ago');
    expect(formatRelativeTime(iso, NOW_MS + MINUTE)).toBe('16 minutes ago');
    expect(formatRelativeTime(iso, NOW_MS + HOUR)).toBe('1 hour ago');
  });
});

describe('parseInstant', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** 2026-09-24T11:40:00.000Z, with `ms` milliseconds added. Written out, not parsed. */
  const at = (ms: number): number => Date.UTC(2026, 8, 24, 11, 40, 0, ms);

  it.each([
    ['2026-09-24T11:40:00.123456Z', at(123)],
    ['2026-09-24T11:40:00.123999Z', at(123)],
    ['2026-09-24T11:40:00.999999Z', at(999)],
    ['2026-09-24T11:40:00.1234567891Z', at(123)],
    ['2026-09-24T11:40:00.123Z', at(123)],
    ['2026-09-24T11:40:00.5Z', at(500)],
    ['2026-09-24T11:40:00Z', at(0)],
    ['2026-09-24T13:40:00.123456+02:00', at(123)],
  ])('reads %s as the millisecond %i', (iso, expected) => {
    expect(parseInstant(iso)).toBe(expected);
  });

  it('truncates the fraction rather than rounding it', () => {
    // .999999 must stay in the same second: rounding would carry it into the
    // next one, and the next minute, hour and day with it at the edges.
    expect(parseInstant('2026-09-24T23:59:59.999999Z')).toBe(
      Date.UTC(2026, 8, 24, 23, 59, 59, 999),
    );
  });

  it('hands Date only the three-digit fraction ECMAScript guarantees', () => {
    // What an engine does with six fractional digits is implementation-
    // defined; V8 happens to truncate, which is why no behavioural assertion
    // above can tell a truncating parseInstant from one that passes the raw
    // string through. This pins what is actually handed to `Date`.
    const seen: unknown[] = [];
    const RealDate = Date;
    vi.stubGlobal(
      'Date',
      class extends RealDate {
        constructor(...args: [string | number]) {
          seen.push(args[0]);
          super(...args);
        }
      },
    );

    parseInstant('2026-09-24T11:40:00.123456+02:00');

    expect(seen).toEqual(['2026-09-24T11:40:00.123+02:00']);
  });

  it('leaves a timestamp with no excess digits untouched', () => {
    const seen: unknown[] = [];
    const RealDate = Date;
    vi.stubGlobal(
      'Date',
      class extends RealDate {
        constructor(...args: [string | number]) {
          seen.push(args[0]);
          super(...args);
        }
      },
    );

    parseInstant('2026-09-24T11:40:00Z');
    parseInstant('2026-09-24T11:40:00.12Z');

    expect(seen).toEqual(['2026-09-24T11:40:00Z', '2026-09-24T11:40:00.12Z']);
  });
});

describe('formatAbsoluteTime', () => {
  it('renders the date in English, whatever the machine locale', () => {
    const text = formatAbsoluteTime('2026-09-24T12:00:00.000Z');

    expect(text).toMatch(/2026/);
    expect(text).toMatch(/Sep/);
  });
});

/**
 * Newer ICU puts a narrow no-break space (U+202F) before AM/PM. The DOM
 * matchers collapse it with every other space, so the pure tests do too, and
 * both compare against the spec's wording with ordinary spaces.
 */
function plain(text: string): string {
  return text.replace(/\s+/gu, ' ');
}

describe('formatHistoryStart', () => {
  it.each([
    ['2026-06-27T12:05:36Z', 'Jun 27, 2026, 12:05:36 PM UTC'],
    ['2026-06-27T12:05:36.000000Z', 'Jun 27, 2026, 12:05:36 PM UTC'],
    ['2026-06-27T12:05:36.000Z', 'Jun 27, 2026, 12:05:36 PM UTC'],
  ])('names the whole second %s as itself: %j', (iso, expected) => {
    expect(plain(formatHistoryStart(iso))).toBe(expected);
  });

  it.each([
    // `clamp_to_retention` floors to a whole millisecond and the backend
    // serialises with microseconds, so this is the form the banner meets.
    ['2026-06-27T12:05:36.250000Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
    ['2026-06-27T12:05:36.001Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
    ['2026-06-27T12:05:36.500Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
    ['2026-06-27T12:05:36.999999Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
    // A hundred microseconds past the second: below the millisecond, but not
    // at its last digit.
    ['2026-06-27T12:05:36.0001Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
    ['2026-06-27T12:05:36.000100Z', 'Jun 27, 2026, 12:05:37 PM UTC'],
  ])('rounds the sub-second instant %s up to the next second: %j', (iso, expected) => {
    // Rounding down, or to nearest, would name an instant earlier than the
    // one the history is complete from, and claim a trade in the lost
    // fraction of a second as held.
    expect(plain(formatHistoryStart(iso))).toBe(expected);
  });

  it('rounds up an instant a single microsecond past a whole second', () => {
    // The spec's rule is "never earlier than the true instant". A parse that
    // truncates to milliseconds first would read .000001 as .000 and name the
    // second before it.
    expect(plain(formatHistoryStart('2026-06-27T12:05:36.000001Z'))).toBe(
      'Jun 27, 2026, 12:05:37 PM UTC',
    );
  });

  it('carries a rounded-up second into the next minute, day and year', () => {
    expect(plain(formatHistoryStart('2026-12-31T23:59:59.500000Z'))).toBe(
      'Jan 1, 2027, 12:00:00 AM UTC',
    );
  });

  it('reads an instant with an offset as that instant, and names it in UTC', () => {
    expect(plain(formatHistoryStart('2026-06-27T14:05:36.250+02:00'))).toBe(
      'Jun 27, 2026, 12:05:37 PM UTC',
    );
  });

  it('does not read the offset as fractional digits of a whole second', () => {
    // "+02:00" after ".000" has a nonzero digit, and it is not a fraction.
    expect(plain(formatHistoryStart('2026-06-27T14:05:36.000+02:00'))).toBe(
      'Jun 27, 2026, 12:05:36 PM UTC',
    );
    expect(plain(formatHistoryStart('2026-06-27T14:05:36.000000+02:00'))).toBe(
      'Jun 27, 2026, 12:05:36 PM UTC',
    );
    expect(plain(formatHistoryStart('2026-06-27T10:05:36.000-02:00'))).toBe(
      'Jun 27, 2026, 12:05:36 PM UTC',
    );
  });

  it('names the instant in UTC whatever the machine zone', () => {
    // `PORTFOLIO_EXCHANGE_HISTORY_START` is read in UTC, so the banner names
    // UTC too. CI runs in UTC, where a formatter that forgot the zone would
    // still pass; moving the zone is what makes this test mean something.
    const originalZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const originalTz = process.env.TZ;
    const instant = '2026-06-27T12:05:36.250000Z';

    try {
      process.env.TZ = 'Pacific/Kiritimati';
      // Positive control: the zone really moved (UTC+14, so the next day).
      expect(new Date(instant).getDate()).toBe(28);
      expect(plain(formatHistoryStart(instant))).toBe('Jun 27, 2026, 12:05:37 PM UTC');

      process.env.TZ = 'America/Los_Angeles';
      // UTC-7 in June.
      expect(new Date(instant).getHours()).toBe(5);
      expect(plain(formatHistoryStart(instant))).toBe('Jun 27, 2026, 12:05:37 PM UTC');
    } finally {
      process.env.TZ = originalZone;
      if (originalTz === undefined) {
        delete process.env.TZ;
      } else {
        process.env.TZ = originalTz;
      }
    }
  });

  it('renders English month names and a 12-hour clock', () => {
    expect(plain(formatHistoryStart('2026-09-24T12:00:00Z'))).toBe('Sep 24, 2026, 12:00:00 PM UTC');
  });
});

describe('formatHistoryStartLocal', () => {
  it('names the same rounded-up instant in local time, with seconds and the zone', () => {
    // R13: the banner's title. Built from the raw instant, it would name
    // 8:05 while the text says 12:06:00 UTC - an earlier minute.
    inTimeZone('America/New_York');
    // Positive control: UTC-4 in June.
    expect(new Date('2026-06-27T12:05:59.500Z').getHours()).toBe(8);

    expect(plain(formatHistoryStartLocal('2026-06-27T12:05:59.500000Z'))).toBe(
      'Jun 27, 2026, 8:06:00 AM EDT',
    );
    expect(plain(formatHistoryStartLocal('2026-06-27T12:05:36Z'))).toBe(
      'Jun 27, 2026, 8:05:36 AM EDT',
    );
    expect(plain(formatHistoryStartLocal('2026-06-27T12:05:36.000001Z'))).toBe(
      'Jun 27, 2026, 8:05:37 AM EDT',
    );
  });

  it('follows the machine zone, unlike the UTC text', () => {
    inTimeZone('Asia/Tokyo');

    expect(plain(formatHistoryStartLocal('2026-06-27T12:05:36.250000Z'))).toBe(
      'Jun 27, 2026, 9:05:37 PM GMT+9',
    );
  });
});

describe('formatDuration', () => {
  it.each([
    [0, 'under a second'],
    [1, 'under a second'],
    [999, 'under a second'],
  ])('renders %i ms as %j', (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it.each([
    [1_000, '1 second'],
    [1_999, '1 second'],
    [2_000, '2 seconds'],
    [30_500, '30 seconds'],
    [59_999, '59 seconds'],
  ])('renders %i ms, under a minute, as whole seconds floored: %j', (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it.each([
    [60_000, '1 minute'],
    [60_999, '1 minute'],
    [61_000, '1 minute 1 second'],
    [62_000, '1 minute 2 seconds'],
    [120_000, '2 minutes'],
    [121_000, '2 minutes 1 second'],
    [125_999, '2 minutes 5 seconds'],
    [3_599_999, '59 minutes 59 seconds'],
  ])('renders %i ms, under an hour, as minutes then seconds when any: %j', (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });

  it.each([
    [3_600_000, '1 hour'],
    [3_659_999, '1 hour'],
    [3_660_000, '1 hour 1 minute'],
    [3_720_000, '1 hour 2 minutes'],
    [7_200_000, '2 hours'],
    [7_260_000, '2 hours 1 minute'],
    [7_325_000, '2 hours 2 minutes'],
    [5_399_999, '1 hour 29 minutes'],
    [5_400_000, '1 hour 30 minutes'],
    [9_000_000, '2 hours 30 minutes'],
    [90_000_000, '25 hours'],
  ])('renders %i ms, an hour or more, as hours then minutes when any: %j', (ms, expected) => {
    // Seconds are dropped past an hour: a first backfill measured in hours
    // does not need them.
    expect(formatDuration(ms)).toBe(expected);
  });
});

describe('useNow', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('starts at the current time and re-renders on every tick', () => {
    // `setInterval` is faked along with `Date` so the tick can be driven by
    // hand; `setTimeout` is left real because nothing here needs it and the
    // rest of the suite's async machinery does.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(NOW_MS);

    const { result } = renderHook(() => useNow(30_000));

    expect(result.current).toBe(NOW_MS);

    act(() => {
      vi.advanceTimersByTime(29_999);
    });
    expect(result.current).toBe(NOW_MS);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(result.current).toBe(NOW_MS + 30_000);

    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    expect(result.current).toBe(NOW_MS + 60_000);
  });

  it('stops ticking when it unmounts', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(NOW_MS);

    const { unmount } = renderHook(() => useNow(30_000));
    expect(vi.getTimerCount()).toBe(1);

    unmount();

    // An interval that outlives its component keeps a dead tab busy forever.
    expect(vi.getTimerCount()).toBe(0);
  });

  it('restarts its interval when the period changes', () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(NOW_MS);

    const { result, rerender } = renderHook(({ period }) => useNow(period), {
      initialProps: { period: 30_000 },
    });

    rerender({ period: 5_000 });
    expect(vi.getTimerCount()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(result.current).toBe(NOW_MS + 5_000);
  });
});
