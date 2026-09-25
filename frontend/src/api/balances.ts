/**
 * Fetchers and hooks for the balance endpoints: `GET /api/balances/current`,
 * `GET /api/balances/runs`, `POST /api/balances/sync`.
 *
 * Types come from the generated schema and nowhere else, per CLAUDE.md rule "Types come
 * from the backend."
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type CurrentBalances = components['schemas']['CurrentBalancesResponse'];
export type WalletBalance = components['schemas']['WalletBalanceResponse'];
export type SyncRun = components['schemas']['SyncRunResponse'];
export type SyncTriggered = components['schemas']['SyncTriggeredResponse'];

const CURRENT_BALANCES_PATH = '/api/balances/current';
const RUNS_PATH = '/api/balances/runs';
const SYNC_PATH = '/api/balances/sync';

/** How many runs `useSyncRuns` asks for: exactly what the freshness rule needs. */
const RUNS_LIMIT = 2;

/**
 * Both balance queries poll every minute. Neither endpoint reaches a vendor - `current`
 * reads the snapshot and price tables, `runs` reads the run log - so polling costs a
 * database read, not a rate-limited request. Without it, a dashboard left open never shows
 * the scheduler's next tick, and "last updated" would be the only thing on the page that
 * ages.
 */
const REFETCH_INTERVAL_MS = 60_000;

export const currentBalancesQueryKey = ['balances', 'current'] as const;
export const runsQueryKey = ['balances', 'runs'] as const;

export function useCurrentBalances(): UseQueryResult<CurrentBalances> {
  return useQuery({
    queryKey: currentBalancesQueryKey,
    queryFn: ({ signal }) => apiFetch<CurrentBalances>(CURRENT_BALANCES_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/** Newest first, per the endpoint's contract - {@link selectSettledRun} relies on that order. */
export function useSyncRuns(): UseQueryResult<SyncRun[]> {
  return useQuery({
    queryKey: runsQueryKey,
    queryFn: async ({ signal }) => {
      const response = await apiFetch<components['schemas']['SyncRunListResponse']>(
        `${RUNS_PATH}?limit=${String(RUNS_LIMIT)}`,
        { signal },
      );
      return response.runs;
    },
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/**
 * Triggers a sync and returns its summary. On success, invalidates every `['balances', ...]`
 * query - both `current` and `runs` - so the refreshed reading and the updated run log
 * appear together rather than one query racing the other's refetch.
 */
export function useSyncBalances(): UseMutationResult<SyncTriggered, unknown, void> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<SyncTriggered>(SYNC_PATH, { method: 'POST' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['balances'] }),
  });
}
