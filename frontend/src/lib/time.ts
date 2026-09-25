/**
 * Relative time: a ticking clock hook plus the pure formatting it feeds `<RelativeTime>`.
 *
 * Locale is fixed to `en` throughout, because UI strings are English (CLAUDE.md rule 1).
 */
import { useEffect, useState } from 'react';

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
