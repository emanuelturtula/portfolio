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

export type PortfolioChanges = components['schemas']['PortfolioChangesResponse'];
export type Change = components['schemas']['ChangeResponse'];
export type ChangePeriod = components['schemas']['ChangePeriod'];
export type ChangeUnavailable = components['schemas']['Unavailable'];

export const PORTFOLIO_CHANGES_PATH = '/api/portfolio/changes';

/** Polled at the summary's cadence, for the summary's reason: its value now is the total. */
const REFETCH_INTERVAL_MS = 60_000;

/** Under `['portfolio', ...]`, which a balance sync and a wallet change invalidate. */
export const portfolioChangesQueryKey = ['portfolio', 'changes'] as const;

export function usePortfolioChanges(): UseQueryResult<PortfolioChanges> {
  return useQuery({
    queryKey: portfolioChangesQueryKey,
    queryFn: ({ signal }) => apiFetch<PortfolioChanges>(PORTFOLIO_CHANGES_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
