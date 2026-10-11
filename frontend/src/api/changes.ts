/**
 * Fetcher and hook for `GET /api/portfolio/changes`: the value's change over the last 24 hours
 * and 7 days (spec 041).
 *
 * Types come from the generated schema and nowhere else. Every amount and percentage is a
 * string, and a change nothing could work out is `null` with its reason, never `"0"`.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';
import { usePortfolioScope, withPortfolio } from '@/lib/portfolioScope';

export type PortfolioChanges = components['schemas']['PortfolioChangesResponse'];
export type Change = components['schemas']['ChangeResponse'];
export type ChangePeriod = components['schemas']['ChangePeriod'];
export type ChangeUnavailable = components['schemas']['Unavailable'];

export const PORTFOLIO_CHANGES_PATH = '/api/portfolio/changes';

/** Polled at the summary's cadence, for the summary's reason: its value now is the total. */
const REFETCH_INTERVAL_MS = 60_000;

/**
 * Under `['portfolio', ...]`, which a balance sync and a wallet change invalidate; one entry
 * per portfolio scope (spec 044).
 */
export function portfolioChangesQueryKey(scope: number | null) {
  return ['portfolio', 'changes', scope] as const;
}

export function usePortfolioChanges(): UseQueryResult<PortfolioChanges> {
  const { scope } = usePortfolioScope();
  return useQuery({
    queryKey: portfolioChangesQueryKey(scope),
    queryFn: ({ signal }) =>
      apiFetch<PortfolioChanges>(withPortfolio(PORTFOLIO_CHANGES_PATH, scope), { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
