/**
 * Relative time: a ticking clock hook plus the pure formatting it feeds `<RelativeTime>`.
 *
 * Locale is fixed to `en` throughout, because UI strings are English (CLAUDE.md rule 1).
 */
import { useEffect, useState } from 'react';

const SECOND_MS = 1_000;
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const JUST_NOW_THRESHOLD_MS = 45_000;
const MINUTES_THRESHOLD_MS = 45 * MINUTE_MS;
const HOURS_THRESHOLD_MS = 22 * HOUR_MS;

/**
 * The current time in milliseconds since the epoch, re-rendering every `intervalMs`.
 *
 * Without this tick, a relative label such as "just now" would stay on screen for as long
 * as the tab stays open - exactly the kind of truthful-looking stale label this page exists
 * to avoid. Reads `Date.now()` rather than constructing a `Date` object, since only the
 * instant is needed.
 */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now());
    }, intervalMs);
    return () => {
      clearInterval(id);
    };
  }, [intervalMs]);

  return now;
}

/**
 * Matches a fractional-seconds part longer than milliseconds - `.123456`, not `.123` - and
 * captures the first three digits, so {@link parseInstant} can drop the rest.
 */
const EXCESS_FRACTIONAL_SECONDS_PATTERN = /(\.\d{3})\d+/;

/**
 * Parses an ISO-8601 instant into milliseconds since the epoch, truncating a fractional
 * part longer than milliseconds before handing it to `Date`.
 *
 * The backend stamps `observed_at` and the run log's timestamps with microsecond
 * precision (`.123456Z`), but the ECMAScript specification only guarantees `Date` parsing
 * for *its own* format, which carries exactly three fractional digits - what an engine
 * does with six is implementation-defined. Truncating rather than rounding is deliberate:
 * rounding `.123999` up to `.124` could move an instant a millisecond later than it really
 * is, which is exactly backwards for the one comparison this exists to keep correct -
 * `observed_at >= started_at` - since a chain's read is stamped at or after its run's
 * start and must never be made to look earlier than it, nor pushed past a boundary it
 * did not actually cross.
 */
export function parseInstant(iso: string): number {
  return new Date(iso.replace(EXCESS_FRACTIONAL_SECONDS_PATTERN, '$1')).getTime();
}

/**
 * Renders the gap between `iso` and `nowMs` as a short phrase, for example "5 minutes ago".
 * A negative gap - clock skew, or a timestamp stamped a moment after `nowMs` was read - is
 * floored to zero rather than printed as a time in the future.
 */
export function formatRelativeTime(iso: string, nowMs: number): string {
  const diffMs = Math.max(0, nowMs - parseInstant(iso));

  if (diffMs < JUST_NOW_THRESHOLD_MS) {
    return 'just now';
  }

  if (diffMs < MINUTES_THRESHOLD_MS) {
    const minutes = Math.round(diffMs / MINUTE_MS);
    return `${String(minutes)} minute${minutes === 1 ? '' : 's'} ago`;
  }

  if (diffMs < HOURS_THRESHOLD_MS) {
    const hours = Math.round(diffMs / HOUR_MS);
    return `${String(hours)} hour${hours === 1 ? '' : 's'} ago`;
  }

  const days = Math.round(diffMs / DAY_MS);
  return `${String(days)} day${days === 1 ? '' : 's'} ago`;
}

/** The absolute instant `iso` names, for a `title` attribute alongside the relative text. */
export function formatAbsoluteTime(iso: string): string {
  return new Date(parseInstant(iso)).toLocaleString('en', {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

/** Renders `count` followed by `noun`, pluralised with a trailing "s" when `count` is not 1. */
function plural(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? '' : 's'}`;
}

/**
 * Renders a duration in milliseconds as a short phrase, for a sync run's `duration_ms` -
 * see docs/specs/016-exchanges-page.md's formatting table. `duration_ms` is a plain integer
 * the backend already computed, never a monetary value, so there is nothing here for the
 * money-coercion lint rule to object to.
 *
 * | Input | Output |
 * |---|---|
 * | under 1,000 ms | "under a second" |
 * | under a minute | "N second(s)", floored |
 * | under an hour | "M minute(s)", then " S second(s)" when S > 0 |
 * | an hour or more | "H hour(s)", then " M minute(s)" when M > 0 |
 */
export function formatDuration(ms: number): string {
  if (ms < SECOND_MS) {
    return 'under a second';
  }

  if (ms < MINUTE_MS) {
    return plural(Math.floor(ms / SECOND_MS), 'second');
  }

  if (ms < HOUR_MS) {
    const minutes = Math.floor(ms / MINUTE_MS);
    const seconds = Math.floor((ms % MINUTE_MS) / SECOND_MS);
    return seconds > 0
      ? `${plural(minutes, 'minute')} ${plural(seconds, 'second')}`
      : plural(minutes, 'minute');
  }

  const hours = Math.floor(ms / HOUR_MS);
  const minutes = Math.floor((ms % HOUR_MS) / MINUTE_MS);
  return minutes > 0
    ? `${plural(hours, 'hour')} ${plural(minutes, 'minute')}`
    : plural(hours, 'hour');
}

/** Matches everything from the first non-digit onward - the `Z` or the `+hh:mm` offset. */
const AFTER_FRACTIONAL_DIGITS_PATTERN = /[^0-9].*$/;

/**
 * Whether `iso` carries a nonzero digit *below* millisecond precision - a microsecond
 * remainder `parseInstant` truncates away entirely rather than rounds.
 *
 * `formatHistoryStart` needs this because `parseInstant`'s truncation can land exactly on a
 * whole millisecond - `.000001` truncates to `.000` - which then looks, to a plain
 * `Math.ceil` over its milliseconds, indistinguishable from a *true* whole second. It is
 * not: `.000001` is a moment after `:36.000`, so naming it "36" would still be earlier than
 * the true instant. See {@link formatHistoryStart}'s single-microsecond test case.
 *
 * Plain string slicing rather than a regex capture group, so there is no indexed access
 * into a match array for `noUncheckedIndexedAccess` to (correctly, but untestably) call
 * possibly `undefined`: a capturing group on a `+`-quantified pattern is never actually
 * absent once the overall match succeeds, and a fallback for that impossible case would be
 * a branch no fixture could ever exercise.
 */
function hasSubMillisecondRemainder(iso: string): boolean {
  const dotIndex = iso.indexOf('.');
  if (dotIndex === -1) {
    return false;
  }
  const fractional = iso.slice(dotIndex + 1).replace(AFTER_FRACTIONAL_DIGITS_PATTERN, '');
  return /[1-9]/.test(fractional.slice(3));
}

/**
 * The millisecond instant `formatHistoryStart` and `formatHistoryStartLocal` both name:
 * `iso`, rounded up to the next whole second. Rounding up - `Math.ceil` over whole seconds,
 * not `Math.round` - means the named instant is never earlier than the true one: rounding
 * down a sub-second instant would claim a trade in that truncated fraction as held when it
 * might not be. A microsecond remainder `parseInstant` truncates away is nudged in first -
 * see {@link hasSubMillisecondRemainder} - so it still forces the round-up its own
 * whole-millisecond value would otherwise hide.
 */
function roundedUpToSecond(iso: string): number {
  const ms = parseInstant(iso) + (hasSubMillisecondRemainder(iso) ? 1 : 0);
  return Math.ceil(ms / SECOND_MS) * SECOND_MS;
}

/**
 * Renders the instant `iso` names in UTC, at second precision, **rounded up** to the next
 * whole second - the exact earliest date an exchange's history is complete from
 * (`effective_since`), per docs/specs/016-exchanges-page.md.
 *
 * UTC because that is the zone `PORTFOLIO_EXCHANGE_HISTORY_START` is read in, so the owner
 * compares like with like, and because it makes this independent of the machine's zone.
 */
export function formatHistoryStart(iso: string): string {
  return new Date(roundedUpToSecond(iso)).toLocaleString('en', {
    dateStyle: 'medium',
    timeStyle: 'long',
    timeZone: 'UTC',
  });
}

/**
 * The same rounded-up instant {@link formatHistoryStart} names, in the browser's own local
 * time, with seconds and its zone name - for the truncation banner's `title` (spec R13,
 * `timeStyle: 'long'` added by R19). A `title` built from the *raw*, un-rounded
 * `effective_since` could name a moment a whole second earlier than the rounded-up text it
 * annotates; sharing {@link roundedUpToSecond} is what rules that out. Naming the zone is
 * what tells the owner the tooltip is *not* the UTC instant the visible text already gives.
 */
export function formatHistoryStartLocal(iso: string): string {
  return new Date(roundedUpToSecond(iso)).toLocaleString('en', {
    dateStyle: 'medium',
    timeStyle: 'long',
  });
}
