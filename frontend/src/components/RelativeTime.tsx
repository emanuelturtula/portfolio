import { formatAbsoluteTime, formatRelativeTime, useNow } from '@/lib/time';

interface RelativeTimeProps {
  readonly value: string;
}

/** How often the relative label re-renders. Frequent enough that "just now" does not stale. */
const TICK_MS = 30_000;

/**
 * Renders an ISO instant as `<time dateTime>`, with the exact instant in `title` and a
 * relative phrase - "5 minutes ago" - as the visible text. `useNow` is what keeps the
 * phrase advancing without a reload.
 */
export function RelativeTime({ value }: RelativeTimeProps) {
  const now = useNow(TICK_MS);

  return (
    <time dateTime={value} title={formatAbsoluteTime(value)}>
      {formatRelativeTime(value, now)}
    </time>
  );
}
