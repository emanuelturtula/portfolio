/**
 * Fetcher and hook for the accounting endpoint: `GET /api/accounting/positions`. See
 * docs/specs/021-position-snapshots.md for what the fields mean and
 * docs/specs/022-invested-per-asset-dashboard.md for how the dashboard shows them.
 *
 * Types come from the generated schema and nowhere else, per CLAUDE.md rule "Types come
 * from the backend."
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type Positions = components['schemas']['PositionsResponse'];
export type Position = components['schemas']['AccountingPositionResponse'];
export type PositionTotals = components['schemas']['AccountingTotalsResponse'];
export type Exclusion = components['schemas']['ExclusionResponse'];
export type AccountingWarning = components['schemas']['AccountingWarningResponse'];
export type LastRecompute = components['schemas']['LastRecomputeResponse'];

const POSITIONS_PATH = '/api/accounting/positions';

/**
 * The positions poll every minute. The endpoint reads the stored snapshot and the price
 * table and reaches no vendor, so polling costs a database read, not a rate-limited
 * request. The snapshot itself only changes after an exchange sync that stored a fill, but
 * the prices the positions are valued at move on the balances refresh's schedule.
 */
const REFETCH_INTERVAL_MS = 60_000;

/**
 * `['accounting', ...]` is the prefix an exchange sync invalidates: a sync that stored a
 * fill has already recomputed the snapshot by the time it answers.
 */
export const positionsQueryKey = ['accounting', 'positions'] as const;

export function usePositions(): UseQueryResult<Positions> {
  return useQuery({
    queryKey: positionsQueryKey,
    queryFn: ({ signal }) => apiFetch<Positions>(POSITIONS_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
