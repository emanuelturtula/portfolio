import { formatAbsoluteTime } from '@/lib/time';

interface AbsoluteTimeProps {
  readonly value: string;
}

/**
 * Renders an ISO instant as `<time dateTime>` with the instant itself, e.g. "Sep 29, 2026,
 * 10:00 AM", as the visible text.
 *
 * The counterpart of `<RelativeTime>` for the places a relative phrase does not belong: it
 * ticks, and a ticking phrase inside a live region (`role="alert"`) is re-announced every
 * time it changes (spec 016's lesson). An instant that never changes on screen can sit in
 * one safely, and a warning about *when* something failed still has to say when.
 */
export function AbsoluteTime({ value }: AbsoluteTimeProps) {
  return <time dateTime={value}>{formatAbsoluteTime(value)}</time>;
}
