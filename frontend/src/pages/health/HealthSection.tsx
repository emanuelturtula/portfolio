import type { ReactNode } from 'react';

import { AbsoluteTime } from '@/components/AbsoluteTime';
import { UNAVAILABLE_WORDS } from '@/lib/health';

interface HealthSectionProps {
  /** The heading's id, which the section is named by. */
  readonly headingId: string;
  readonly title: string;
  readonly children: ReactNode;
}

/**
 * One section of the Health page: a landmark named by its `h3`, as Backups is. The items in
 * it are headed `h4`, so the outline reads page, section, item.
 */
export function HealthSection({ headingId, title, children }: HealthSectionProps) {
  return (
    <section className="card" aria-labelledby={headingId}>
      <h3 id={headingId}>{title}</h3>
      {children}
    </section>
  );
}

/**
 * A section the backend could not build. It is an alert, not a state in a list: the section's
 * own data is what could not be read, so there is nothing else to show in its place, and
 * nothing here is shown as `ok` or as zero.
 */
export function Unavailable() {
  return (
    <p className="state state-error" role="alert">
      {UNAVAILABLE_WORDS}
    </p>
  );
}

interface InstantOrNoneProps {
  /** An ISO instant, or `null` when the thing has not happened. */
  readonly value: string | null;
  /** What to say instead of a date, such as "never". */
  readonly none: string;
}

/** An instant as the backup section shows one, or `none` where there is no instant. */
export function InstantOrNone({ value, none }: InstantOrNoneProps) {
  return value === null ? none : <AbsoluteTime value={value} />;
}
