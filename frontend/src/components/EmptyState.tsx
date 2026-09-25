import type { ReactNode } from 'react';

/** A heading level 2 through 6, for nesting inside a section already headed by an `hN`. */
export type HeadingLevel = 2 | 3 | 4 | 5 | 6;

/**
 * Maps a level to its tag name. A lookup rather than a template literal (`` `h${level}` ``)
 * so that JSX gets a plain string-literal-typed value: ESLint's `restrict-template-expressions`
 * does not recognise a numeric-literal union as a safe template interpolant, and the
 * lookup sidesteps that without a type assertion to paper over it.
 */
export const HEADING_TAGS: Record<HeadingLevel, 'h2' | 'h3' | 'h4' | 'h5' | 'h6'> = {
  2: 'h2',
  3: 'h3',
  4: 'h4',
  5: 'h5',
  6: 'h6',
};

interface EmptyStateProps {
  readonly title: string;
  readonly description?: string;
  readonly action?: ReactNode;
  /**
   * The heading's level, defaulting to `h2`. This state is reused inside sections already
   * headed by an `h3` - "Your wallets" - and hard-coding `h2` there would put a level-2
   * heading under a level-3 one, breaking the document outline a screen reader user
   * navigates by.
   */
  readonly headingLevel?: HeadingLevel;
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
export function EmptyState({ title, description, action, headingLevel = 2 }: EmptyStateProps) {
  const Heading = HEADING_TAGS[headingLevel];

  return (
    <div className="state state-empty">
      <Heading>{title}</Heading>
      {description !== undefined && <p>{description}</p>}
      {action}
    </div>
  );
}
