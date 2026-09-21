interface SkeletonProps {
  readonly label?: string;
}

/**
 * Shared loading placeholder. `role="status"` is a polite live region, so a
 * screen reader announces the wait once, rather than the user facing silence
 * while a spinner turns with nothing to say it is there.
 */
export function Skeleton({ label = 'Loading…' }: SkeletonProps) {
  return (
    <p className="state state-loading" role="status">
      {label}
    </p>
  );
}
