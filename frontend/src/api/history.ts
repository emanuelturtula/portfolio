/**
 * Fetchers and hooks for the value history (spec 037): `GET /api/portfolio/history` and
 * `GET /api/wallets/{wallet_id}/value-history`.
 *
 * Types come from the generated schema and nowhere else. Every amount is a string, and a day
 * nothing could value is `null`, never `"0"`: the chart draws it as a gap.
 */
import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type HistoryRange = components['schemas']['HistoryRange'];
export type PortfolioHistory = components['schemas']['PortfolioHistoryResponse'];
export type PortfolioPoint = components['schemas']['PortfolioPointResponse'];
export type WalletValueHistory = components['schemas']['WalletValueHistoryResponse'];
export type WalletPoint = components['schemas']['WalletPointResponse'];

export const PORTFOLIO_HISTORY_PATH = '/api/portfolio/history';

/** The wallet route, by id. Only the id travels in the URL, never an address. */
export function walletValueHistoryPath(walletId: number): string {
  return `/api/wallets/${String(walletId)}/value-history`;
}

/** Every range the endpoints accept, in the order the selector offers them. */
export const HISTORY_RANGES: readonly HistoryRange[] = ['30d', '90d', '1y', 'all'];

/** The endpoints' own default, which the chart starts on. */
export const DEFAULT_HISTORY_RANGE: HistoryRange = '90d';

/**
 * Polled at the summary's cadence: the last point is today, valued from the latest readings
 * and today's price, and a chart that ended on a different figure from the total beside it
 * would leave the owner to guess which one is right. The endpoints read stored rows and reach
 * no vendor.
 */
const REFETCH_INTERVAL_MS = 60_000;

/**
 * Under `['portfolio', ...]`, the prefix a balance sync and a wallet change invalidate: both
 * change what a day is worth.
 */
export function portfolioHistoryQueryKey(range: HistoryRange) {
  return ['portfolio', 'history', range] as const;
}

export function walletValueHistoryQueryKey(walletId: number, range: HistoryRange) {
  return ['portfolio', 'wallet-history', walletId, range] as const;
}

/**
 * What every active wallet together was worth on each day of `range`.
 *
 * A new range keeps the previous one on screen until it arrives (`isPlaceholderData`), so the
 * chart does not collapse to a loading line and back each time the owner changes the range.
 */
export function usePortfolioHistory(range: HistoryRange): UseQueryResult<PortfolioHistory> {
  return useQuery({
    queryKey: portfolioHistoryQueryKey(range),
    queryFn: ({ signal }) =>
      apiFetch<PortfolioHistory>(`${PORTFOLIO_HISTORY_PATH}?range=${range}`, { signal }),
    placeholderData: keepPreviousData,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/**
 * One wallet's quantity and value on each day of `range`.
 *
 * The previous range is kept on screen while a new one loads, as above, but only for the same
 * wallet: another wallet's line under this wallet's name would be a wrong answer, not a stale
 * one.
 */
export function useWalletValueHistory(
  walletId: number,
  range: HistoryRange,
): UseQueryResult<WalletValueHistory> {
  return useQuery({
    queryKey: walletValueHistoryQueryKey(walletId, range),
    queryFn: ({ signal }) =>
      apiFetch<WalletValueHistory>(`${walletValueHistoryPath(walletId)}?range=${range}`, {
        signal,
      }),
    placeholderData: (previous) => (previous?.wallet_id === walletId ? previous : undefined),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
