import { renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { usePortfolioScope, withPortfolio } from '@/lib/portfolioScope';

describe('withPortfolio', () => {
  it('leaves a path alone for all portfolios', () => {
    expect(withPortfolio('/api/wallets?include_archived=true', null)).toBe(
      '/api/wallets?include_archived=true',
    );
  });

  it('adds the portfolio after a query string or as one', () => {
    expect(withPortfolio('/api/portfolio/summary', 2)).toBe('/api/portfolio/summary?portfolio=2');
    expect(withPortfolio('/api/wallets?include_archived=true', 2)).toBe(
      '/api/wallets?include_archived=true&portfolio=2',
    );
  });
});

describe('outside the provider', () => {
  it('is all portfolios, with nothing to choose and nothing a choice changes', () => {
    const { result, rerender } = renderHook(() => usePortfolioScope());
    result.current.choose(2);
    rerender();

    expect(result.current.scope).toBeNull();
    expect(result.current.portfolios).toEqual([]);
    expect(result.current.resolving).toBe(false);
  });
});
