/**
 * Fetchers and hooks for the exchange endpoints: `GET /api/exchanges`,
 * `GET /api/exchanges/runs`, `POST /api/exchanges/sync`.
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

export type Exchange = components['schemas']['ExchangeResponse'];
export type ExchangeRun = components['schemas']['ExchangeSyncRunResponse'];
export type ExchangeSyncTriggered = components['schemas']['ExchangeSyncTriggeredResponse'];

const EXCHANGES_PATH = '/api/exchanges';
const RUNS_PATH = '/api/exchanges/runs';
const SYNC_PATH = '/api/exchanges/sync';

/** How many runs `useExchangeRuns` asks for - the run log's own limit (spec: the last 20). */
export const EXCHANGE_RUNS_LIMIT = 20;

/**
 * The two poll rates every query on this page chooses between. Neither `GET /api/exchanges`
 * nor `GET /api/exchanges/runs` reaches a vendor - both are database reads - so polling at
 * the fast rate costs a read, not a rate-limited request. Fast is what turns the two queries
 * into a progress display while a sync is in flight; slow is what still notices a scheduled
 * run within a minute the rest of the time.
 */
export const FAST_POLL_MS = 5_000;
export const SLOW_POLL_MS = 60_000;

export const exchangesQueryKey = ['exchanges', 'list'] as const;
export const exchangeRunsQueryKey = ['exchanges', 'runs'] as const;

/**
 * How fast `useExchanges` should poll: fast while any venue is `syncing`, or while this
 * page's own sync request is pending - the gap between the `POST` firing and the first fast
 * poll landing, before any venue's `syncing` flag has been observed. Slow otherwise,
 * including before the list has ever loaded (`exchanges === undefined`), when there is
 * nothing yet to show progress for.
 */
export function listRefetchInterval(
  exchanges: readonly Exchange[] | undefined,
  syncPending: boolean,
): number {
  if (syncPending) {
    return FAST_POLL_MS;
  }
  return exchanges?.some((exchange) => exchange.syncing) === true ? FAST_POLL_MS : SLOW_POLL_MS;
}

/**
 * How fast `useExchangeRuns` should poll: fast while this page's own sync request is
 * pending, or while the newest run is `running` **and** the list shows a venue `syncing`
 * (spec R12, added after review). `runs[0]` is the newest run by the endpoint's own contract
 * (newest first).
 *
 * `anySyncing` is the list's own reading, not this query's - the one place a query here
 * reads the other's data. A `running` row with no venue `syncing` is an orphan a failed
 * close-out left behind, swept at the next run rather than polled fast forever: `syncing`
 * is the coordinator's actual in-flight flag, and a stale `running` row on its own is not
 * evidence of one.
 */
export function runsRefetchInterval(
  runs: readonly ExchangeRun[] | undefined,
  syncPending: boolean,
  anySyncing: boolean,
): number {
  if (syncPending) {
    return FAST_POLL_MS;
  }
  return runs?.[0]?.status === 'running' && anySyncing ? FAST_POLL_MS : SLOW_POLL_MS;
}

/**
 * Every configured venue and every venue with an account, sorted by `exchange_key`. Polls
 * per {@link listRefetchInterval}, decided from its own data rather than by watching the
 * runs query - see the spec's "Queries" section for why each query is independent: neither
 * needs the other's data to know how fast to poll.
 */
export function useExchanges(syncPending: boolean): UseQueryResult<Exchange[]> {
  return useQuery({
    queryKey: exchangesQueryKey,
    queryFn: async ({ signal }) => {
      const response = await apiFetch<components['schemas']['ExchangeListResponse']>(
        EXCHANGES_PATH,
        { signal },
      );
      return response.exchanges;
    },
    refetchInterval: (query) => listRefetchInterval(query.state.data, syncPending),
  });
}

/**
 * The last {@link EXCHANGE_RUNS_LIMIT} exchange sync runs, newest first.
 *
 * `anySyncing` - whether any venue in `useExchanges`'s own list is `syncing` - is threaded
 * in by the caller (`ExchangesPage`) rather than read here, since this hook has no reason to
 * know about the list query at all otherwise. See {@link runsRefetchInterval}.
 */
export function useExchangeRuns(
  syncPending: boolean,
  anySyncing: boolean,
): UseQueryResult<ExchangeRun[]> {
  return useQuery({
    queryKey: exchangeRunsQueryKey,
    queryFn: async ({ signal }) => {
      const response = await apiFetch<components['schemas']['ExchangeSyncRunListResponse']>(
        `${RUNS_PATH}?limit=${String(EXCHANGE_RUNS_LIMIT)}`,
        { signal },
      );
      return response.runs;
    },
    refetchInterval: (query) => runsRefetchInterval(query.state.data, syncPending, anySyncing),
  });
}

/**
 * Triggers a sync of every configured venue and returns its summary. Invalidates every
 * `['exchanges', ...]` query on settle - success **or** error - because a cut-off request
 * very often means the run is still going rather than that it never started (the
 * coordinator shields the run from the client connection; see the spec's Risks section), so
 * the list and the run log are worth re-reading either way.
 *
 * Every `['accounting', ...]` query is invalidated with them (spec 022): a sync that stored
 * a fill has already recomputed the position snapshot before it answers (spec 021), so the
 * invested-per-asset figures are worth re-reading at the same moment, not at the next poll.
 */
export function useSyncExchanges(): UseMutationResult<ExchangeSyncTriggered, unknown, void> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<ExchangeSyncTriggered>(SYNC_PATH, { method: 'POST' }),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ['exchanges'] }),
        queryClient.invalidateQueries({ queryKey: ['accounting'] }),
      ]),
  });
}
