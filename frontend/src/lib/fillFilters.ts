/**
 * The transactions view's filters: what the URL holds, how a picked day becomes the instants
 * the API takes, and the query string the fills request sends. See
 * docs/specs/024-exchange-transactions.md.
 *
 * No React anywhere in this module - it is exercised directly by tests and by the components
 * under `src/pages/exchanges/`, the same split `lib/exchanges.ts` uses.
 *
 * **A day is a local day.** The owner picks "1 March", and the API takes instants, so this
 * module turns a day into the browser's local midnight. The end day is inclusive in the UI
 * and the API's `to` is exclusive, so it becomes the *next* local midnight, computed with
 * calendar arithmetic. `midnight + 24 h` is wrong on the two days a year the clocks change.
 */
import { EXCHANGES, type ExchangeKey } from '@/lib/exchanges';

/**
 * How many fills one page holds. Fixed: the owner does not choose it (spec 024). Five, so a
 * page of trades fits a phone's screen beside the totals and the chart above it.
 */
export const FILLS_PAGE_SIZE = 5;

export interface FillFilters {
  /** Empty means every exchange, as the API reads an omitted `exchange`. */
  readonly exchanges: readonly ExchangeKey[];
  /** A local day, `YYYY-MM-DD`, inclusive. */
  readonly fromDay: string | null;
  /** A local day, `YYYY-MM-DD`, inclusive. */
  readonly toDay: string | null;
}

export const NO_FILTERS: FillFilters = { exchanges: [], fromDay: null, toDay: null };

/** Every venue this application knows, in the order the checkboxes and the query use. */
export const EXCHANGE_KEYS: readonly ExchangeKey[] = Object.keys(EXCHANGES).filter(
  (key): key is ExchangeKey => Object.hasOwn(EXCHANGES, key),
);

/**
 * The first and last day a filter accepts. Both are what the date inputs' `min` and `max`
 * say, and both keep the instants the API is sent inside what it can read: a `from` at
 * year 0001 is, east of Greenwich, an instant in year 0, and a `to` of 31 December 9999 has
 * its next midnight in year 10000. Neither can be parsed as a datetime by the backend, and
 * the owner would see "Could not load transactions" for a range no fill can be in. Every
 * day up to `MAX_DAY` has a next day that fits.
 */
export const MIN_DAY = '1970-01-01';
export const MAX_DAY = '9999-12-30';

const DAY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const PAGE_PATTERN = /^[1-9]\d{0,5}$/;
const DIGITS = '0123456789';

function pad(value: number, width: number): string {
  return String(value).padStart(width, '0');
}

/**
 * Local midnight of `day`. A date-time with no offset is read as local time, which is the
 * point: `new Date('2026-03-01')` (date only) would be UTC midnight instead.
 */
function startOfDay(day: string): Date {
  return new Date(`${day}T00:00:00`);
}

function formatDay(date: Date): string {
  return `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1, 2)}-${pad(date.getDate(), 2)}`;
}

/**
 * Whether `value` names a real calendar day. The round trip is what refuses `2026-02-30`,
 * which a lenient parser reads as 2 March, and an invalid `Date` formats as `NaN-NaN-NaN`,
 * which is never equal to the input.
 */
function isCalendarDay(value: string): boolean {
  return DAY_PATTERN.test(value) && formatDay(startOfDay(value)) === value;
}

/**
 * Whether `day` is a real calendar day between {@link MIN_DAY} and {@link MAX_DAY}, and so
 * one a filter may hold and the API may be asked about. `YYYY-MM-DD` orders as text.
 */
export function isSelectableDay(day: string): boolean {
  return isCalendarDay(day) && day >= MIN_DAY && day <= MAX_DAY;
}

function dayOrNull(value: string | null): string | null {
  return value !== null && isSelectableDay(value) ? value : null;
}

/**
 * The digits of a page number as a number, by hand. A page is a count, not money, but the
 * lint rule that keeps money out of floating point bans `Number()` and `parseInt` across
 * `src/**`, and one rule with no exemptions is easier to trust than one with a list.
 */
function digitsToInteger(digits: string): number {
  return Array.from(digits).reduce((sum, digit) => sum * 10 + DIGITS.indexOf(digit), 0);
}

function pageOrFirst(value: string | null): number {
  return value !== null && PAGE_PATTERN.test(value) ? digitsToInteger(value) : 1;
}

/**
 * The filters and the page a URL holds. The URL is user input, so nothing in it is trusted:
 *
 * - an unknown exchange is dropped, a repeated one counts once, and the rest come out in the
 *   order of {@link EXCHANGE_KEYS}, so two URLs that mean the same thing share one query key;
 * - a day that is not a real `YYYY-MM-DD` day between {@link MIN_DAY} and {@link MAX_DAY} is
 *   dropped;
 * - a page that is not a positive integer is the first.
 *
 * An inverted range (`to` before `from`) is *kept*: the form shows it and refuses to send it
 * (see {@link isInvertedRange}), so the owner sees what they typed rather than a silent fix.
 */
export function readFillFilters(searchParams: URLSearchParams): {
  readonly filters: FillFilters;
  readonly page: number;
} {
  const selected = new Set(searchParams.getAll('exchange'));

  return {
    filters: {
      exchanges: EXCHANGE_KEYS.filter((key) => selected.has(key)),
      fromDay: dayOrNull(searchParams.get('from')),
      toDay: dayOrNull(searchParams.get('to')),
    },
    page: pageOrFirst(searchParams.get('page')),
  };
}

/**
 * The URL's query for `filters` and `page`. Page 1 and an unset filter are left out, so the
 * unfiltered first page is a bare `/exchanges`.
 */
export function writeFillFilters(filters: FillFilters, page: number): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of filters.exchanges) {
    params.append('exchange', key);
  }
  if (filters.fromDay !== null) {
    params.set('from', filters.fromDay);
  }
  if (filters.toDay !== null) {
    params.set('to', filters.toDay);
  }
  if (page > 1) {
    params.set('page', String(page));
  }
  return params;
}

/** Whether any filter is set, which is what "Clear filters" clears and "no match" depends on. */
export function hasActiveFilters(filters: FillFilters): boolean {
  return filters.exchanges.length > 0 || filters.fromDay !== null || filters.toDay !== null;
}

/**
 * Whether the end day is before the start day. Refused in the form and never sent. The same
 * day on both ends is a valid, single-day range. `YYYY-MM-DD` orders as text.
 */
export function isInvertedRange(filters: FillFilters): boolean {
  return filters.fromDay !== null && filters.toDay !== null && filters.toDay < filters.fromDay;
}

/** `filters` with `key` selected if it was not, and dropped if it was. */
export function toggleExchange(filters: FillFilters, key: ExchangeKey): FillFilters {
  return {
    ...filters,
    exchanges: filters.exchanges.includes(key)
      ? filters.exchanges.filter((selected) => selected !== key)
      : [...filters.exchanges, key],
  };
}

/** What a date input holds when it is empty, as the `null` the filters use. */
export function dayFromInput(value: string): string | null {
  return value === '' ? null : value;
}

/** The browser's time zone, e.g. `Europe/Madrid`: the zone every picked day is a day in. */
export function displayTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

/** A day as the owner reads it: "Mar 1, 2026". */
export function formatDayLabel(day: string): string {
  return startOfDay(day).toLocaleDateString('en', { dateStyle: 'medium' });
}

export interface InstantRange {
  /** Inclusive: the start of the `from` day, local time. */
  readonly from: string | null;
  /** Exclusive: the start of the day *after* the `to` day, local time. */
  readonly to: string | null;
}

/**
 * The instants for a range of local days, as ISO strings.
 *
 * `to` is the next day's midnight and is found by moving the calendar date on by one, not by
 * adding 24 hours: on the day the clocks go forward or back, a local day is 23 or 25 hours
 * long, and the wrong answer would drop or repeat an hour of trades. See
 * {@link startOfNextDay} for why it is built from the calendar fields.
 */
export function dayRangeToInstants(fromDay: string | null, toDay: string | null): InstantRange {
  return {
    from: fromDay === null ? null : startOfDay(fromDay).toISOString(),
    to: toDay === null ? null : startOfNextDay(toDay).toISOString(),
  };
}

/**
 * Local midnight of the calendar day after `day`, found from `day`'s own year, month and date.
 *
 * Not from the `Date` `startOfDay` returns: where the clocks change *at* midnight (Santiago
 * on 6 September 2026 jumps from 00:00 to 01:00), that `Date` reads 01:00, and moving its date
 * on by one carries the 01:00 with it - 7 September 01:00, an hour after the true midnight.
 * Two adjacent days would then overlap by an hour. Setting the calendar fields on a fresh
 * midnight and letting the platform resolve that local time leaves no gap and no overlap.
 * `setFullYear` also keeps a year below 100 as written, where `new Date(y, m, d)` reads it as
 * 19xx.
 */
function startOfNextDay(day: string): Date {
  const next = new Date(2000, 0, 1);
  next.setFullYear(
    digitsToInteger(day.slice(0, 4)),
    digitsToInteger(day.slice(5, 7)) - 1,
    digitsToInteger(day.slice(8, 10)) + 1,
  );
  return next;
}

/** The query string of `GET /api/exchanges/fills` for one page of `filters`. */
export function fillsQuery(filters: FillFilters, page: number): URLSearchParams {
  const { from, to } = dayRangeToInstants(filters.fromDay, filters.toDay);
  const params = new URLSearchParams();
  for (const key of filters.exchanges) {
    params.append('exchange', key);
  }
  if (from !== null) {
    params.set('from', from);
  }
  if (to !== null) {
    params.set('to', to);
  }
  params.set('limit', String(FILLS_PAGE_SIZE));
  params.set('offset', String((page - 1) * FILLS_PAGE_SIZE));
  return params;
}
