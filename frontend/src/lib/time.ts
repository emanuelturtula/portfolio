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
 * Renders the gap between `iso` and `nowMs` as a short phrase, for example "5 minutes ago".
 * A negative gap - clock skew, or a timestamp stamped a moment after `nowMs` was read - is
 * floored to zero rather than printed as a time in the future.
 */
export function formatRelativeTime(iso: string, nowMs: number): string {
  const diffMs = Math.max(0, nowMs - new Date(iso).getTime());

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
  return new Date(iso).toLocaleString('en', {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}
