/**
 * Fetcher and hook for `GET /api/health/detail`: the state of the background work the
 * backend does for the owner, namely the scheduled SQLite backups (docs/specs/029-sqlite-backups.md),
 * and the timers, the balance sync and the prices (docs/specs/030-observability.md). `GET /api/health`, the cheap public liveness check the
 * Health page also calls, keeps its own query on that page.
 *
 * Types come from the generated schema and nowhere else, per CLAUDE.md rule "Types come
 * from the backend."
 */
import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type BackupStatus = components['schemas']['BackupStatusResponse'];
export type BackupState = components['schemas']['BackupState'];
export type BackupErrorKind = components['schemas']['BackupErrorKind'];

export type HealthDetail = components['schemas']['HealthDetailResponse'];
export type SchedulerStatus = components['schemas']['SchedulerStatusResponse'];
export type SchedulerName = components['schemas']['SchedulerName'];
export type SchedulerState = components['schemas']['SchedulerState'];
export type SectionState = components['schemas']['SectionState'];
export type SourceState = components['schemas']['SourceState'];
export type ChainHealth = components['schemas']['ChainHealthResponse'];
export type ChainsHealth = components['schemas']['ChainsHealthResponse'];
export type PricesHealth = components['schemas']['PricesHealthResponse'];
export type PriceHealthState = components['schemas']['PriceHealthState'];

const HEALTH_DETAIL_PATH = '/api/health/detail';

/**
 * The detail polls every minute, like the other read-only endpoints. It lists a directory
 * and reads one in-memory record, so polling costs nothing. Without it, a dashboard left
 * open never shows a backup that failed overnight, and the Health page would keep saying
 * "pending" after the first backup finished.
 */
const REFETCH_INTERVAL_MS = 60_000;

/**
 * Under `['health', ...]`, beside the liveness check's `['health']`. The dashboard's notice
 * and the Health page's section read this one entry, so they share one request.
 */
export const healthDetailQueryKey = ['health', 'detail'] as const;

export function useHealthDetail(): UseQueryResult<HealthDetail> {
  return useQuery({
    queryKey: healthDetailQueryKey,
    queryFn: ({ signal }) => apiFetch<HealthDetail>(HEALTH_DETAIL_PATH, { signal }),
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}
