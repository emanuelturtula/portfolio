interface ErrorStateProps {
  readonly title?: string;
  readonly description: string;
  readonly onRetry?: () => void;
  readonly retryLabel?: string;
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
}: ErrorStateProps) {
  return (
    <div className="state state-error" role="alert">
      <h2>{title}</h2>
      <p>{description}</p>
      {onRetry !== undefined && (
        <button type="button" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  );
}
