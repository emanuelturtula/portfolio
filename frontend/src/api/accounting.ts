/**
 * Fetchers and hooks for the accounting endpoints: `GET /api/accounting/positions` and
 * `GET /api/accounting/reconciliation`. See docs/specs/021-position-snapshots.md for what the
 * positions' fields mean, docs/specs/022-invested-per-asset-dashboard.md for how the
 * dashboard shows them, and docs/specs/025-holdings-reconciliation.md for the reconciliation.
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

export type Reconciliation = components['schemas']['ReconciliationResponse'];
export type ReconciliationAsset = components['schemas']['AssetReconciliationResponse'];
export type ReconciliationExchange = components['schemas']['ExchangeBalancesResponse'];
export type ReconciliationWallets = components['schemas']['WalletsReadResponse'];
export type ReconciliationStatus = components['schemas']['ReconciliationStatus'];

const POSITIONS_PATH = '/api/accounting/positions';
const RECONCILIATION_PATH = '/api/accounting/reconciliation';

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

export const reconciliationQueryKey = ['accounting', 'reconciliation'] as const;

export function usePositions(): UseQueryResult<Positions> {
  return useQuery({
    queryKey: positionsQueryKey,
    queryFn: ({ signal }) => apiFetch<Positions>(POSITIONS_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

/**
 * The holdings check: what the replay says is held against the balances read from the
 * wallets and the exchanges' spot accounts. A database read that reaches no vendor, polled
 * like the positions, and under the same `['accounting']` prefix, so a sync that stores a
 * fill refreshes it with them. See docs/specs/025-holdings-reconciliation.md.
 */
export function useReconciliation(): UseQueryResult<Reconciliation> {
  return useQuery({
    queryKey: reconciliationQueryKey,
    queryFn: ({ signal }) => apiFetch<Reconciliation>(RECONCILIATION_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
