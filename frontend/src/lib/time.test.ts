import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { formatAbsoluteTime, formatRelativeTime, parseInstant, useNow } from '@/lib/time';

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
