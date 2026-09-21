import type { ReactNode } from 'react';

interface EmptyStateProps {
  readonly title: string;
  readonly description?: string;
  readonly action?: ReactNode;
}

/**
 * Shared "you have not added anything yet" state.
 *
 * This is not a live region and carries no `role`: it is ordinary content
 * rendered on a normal successful load, not a transition a screen-reader
 * user needs interrupted for. That is also what separates it from
 * {@link ErrorState} - an empty state means the sync worked and found
 * nothing; an error state means the sync did not work. Rendering a failed
 * sync as an empty list would tell the user the opposite of what happened.
 */
export function EmptyState({ title, description, action }: EmptyStateProps) {
  return (
    <div className="state state-empty">
      <h2>{title}</h2>
      {description !== undefined && <p>{description}</p>}
      {action}
    </div>
  );
}
