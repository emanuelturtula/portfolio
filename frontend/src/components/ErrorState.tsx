import type { ReactNode } from 'react';

import { HEADING_TAGS, type HeadingLevel } from '@/components/EmptyState';

interface ErrorStateProps {
  readonly title?: string;
  /**
   * A string, or inline content when the sentence needs an element in it, such as a `<time>`.
   * Rendered inside a single `<p>`.
   */
  readonly description: ReactNode;
  readonly onRetry?: () => void;
  readonly retryLabel?: string;
  /** A link or control to offer next to the retry button, e.g. the way to the fix. */
  readonly action?: ReactNode;
  /**
   * The heading's level, defaulting to `h2`. See {@link HeadingLevel} and the same prop on
   * `EmptyState` - both states are reused inside sections already headed by an `h3`.
   */
  readonly headingLevel?: HeadingLevel;
}

/**
 * Shared "the sync failed" state. `role="alert"` is what makes this an
 * assertive live region: a screen reader announces it the moment it mounts,
 * without the user having to find it.
 *
 * Deliberately distinct from {@link EmptyState}: a failure to read data and an
 * honest absence of data mean opposite things and must never render the same.
 */
export function ErrorState({
  title = 'Something went wrong',
  description,
  onRetry,
  retryLabel = 'Try again',
  action,
  headingLevel = 2,
}: ErrorStateProps) {
  const Heading = HEADING_TAGS[headingLevel];

  return (
    <div className="state state-error" role="alert">
      <Heading>{title}</Heading>
      <p>{description}</p>
      {onRetry !== undefined && (
        <button type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
      {action}
    </div>
  );
}
