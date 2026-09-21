import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Skeleton } from '@/components/Skeleton';

/** The visible or assistive text a skeleton carries, whichever it uses. */
function announcement(element: HTMLElement): string {
  return element.getAttribute('aria-label') ?? element.textContent;
}

describe('Skeleton', () => {
  it('announces the wait through a live region', () => {
    render(<Skeleton />);

    // A placeholder that is only a grey rectangle tells a screen-reader user
    // nothing at all, so the wait has to say something.
    const status = screen.getByRole('status');
    expect(status).toBeInTheDocument();
    expect(announcement(status).trim().length).toBeGreaterThan(0);
  });

  it('uses the label the caller gives it', () => {
    render(<Skeleton label="Checking your session..." />);

    expect(announcement(screen.getByRole('status'))).toContain('Checking your session...');
  });
});
