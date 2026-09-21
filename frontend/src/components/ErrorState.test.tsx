import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ErrorState } from '@/components/ErrorState';

describe('ErrorState', () => {
  it('announces the failure through an alert region', () => {
    render(
      <ErrorState
        title="The backend is unreachable"
        description="Check that the API is running."
      />,
    );

    // `role="alert"` is an assertive live region: a screen reader announces the
    // failure instead of leaving the user staring at a page that stopped.
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('The backend is unreachable');
    expect(alert).toHaveTextContent('Check that the API is running.');
    // The summary is a heading, not another paragraph. That is what every
    // caller inherits by using this instead of writing its own alert, and it
    // is what `LoginPage.test.tsx` keys on to prove it uses the shared one.
    expect(within(alert).getByRole('heading')).toHaveTextContent('The backend is unreachable');
  });

  it('renders without a retry button when there is nothing to retry', () => {
    render(<ErrorState description="Something went wrong." />);

    expect(screen.getByRole('alert')).toHaveTextContent('Something went wrong.');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('calls onRetry when the retry button is pressed', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();

    render(<ErrorState description="The session could not be read." onRetry={onRetry} />);

    await user.click(screen.getByRole('button', { name: /try again/i }));

    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});
