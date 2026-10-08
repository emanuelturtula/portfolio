/**
 * Fetcher and hook for `GET /api/portfolio/summary`: the dashboard's figures (#154).
 *
 * Types come from the generated schema and nowhere else. Every amount is a string; the client
 * sums nothing.
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type PortfolioSummary = components['schemas']['PortfolioSummaryResponse'];
export type Holding = components['schemas']['HoldingResponse'];
export type MissingPiece = components['schemas']['MissingResponse'];
export type MissingKind = components['schemas']['MissingKind'];

export const PORTFOLIO_SUMMARY_PATH = '/api/portfolio/summary';

/**
 * Polled every minute, like the balances: the endpoint reads stored balances and prices and
 * reaches no vendor, so a poll costs a database read.
 */
const REFETCH_INTERVAL_MS = 60_000;

/**
 * `['portfolio', ...]` is the prefix a balance sync and a wallet change invalidate, since each
 * changes what the summary reads.
 */
export const portfolioSummaryQueryKey = ['portfolio', 'summary'] as const;

export function usePortfolioSummary(): UseQueryResult<PortfolioSummary> {
  return useQuery({
    queryKey: portfolioSummaryQueryKey,
    queryFn: ({ signal }) => apiFetch<PortfolioSummary>(PORTFOLIO_SUMMARY_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
