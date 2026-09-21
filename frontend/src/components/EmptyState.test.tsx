import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EmptyState } from '@/components/EmptyState';

describe('EmptyState', () => {
  it('renders its title and description', () => {
    render(<EmptyState title="No wallets yet" description="Add an address to see a balance." />);

    expect(screen.getByText('No wallets yet')).toBeInTheDocument();
    expect(screen.getByText('Add an address to see a balance.')).toBeInTheDocument();
  });

  it('renders with a title alone', () => {
    render(<EmptyState title="Nothing to show" />);

    expect(screen.getByText('Nothing to show')).toBeInTheDocument();
  });

  it('is not announced as an error or a loading state', () => {
    render(<EmptyState title="No wallets yet" description="Add an address to see a balance." />);

    // "There is nothing here" is ordinary content, not a failure and not a
    // transition. Announcing it as either is how a live region becomes noise
    // that users switch off.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
