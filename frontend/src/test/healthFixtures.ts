import type {
  ChainHealth,
  ChainsHealth,
  HealthDetail,
  PricesHealth,
  SchedulerStatus,
} from '@/api/health';

/**
 * The sections `GET /api/health/detail` serves beside `backup` (spec 030), one fixture per
 * state. Instants are in UTC with a `Z`, as the API serialises them, each distinct from the
 * others, so a value shown under the wrong label cannot pass for the right one.
 *
 * No backup here: `backupFixtures.ts` builds the whole document from these and its own
 * backup fixtures, so neither module imports the other's dependents.
 */

/** A timer's last finished tick: 2026-10-03 at 11:45 UTC. */
export const LAST_TICK_AT = '2026-10-03T11:45:00Z';
/** A chain's last successful read: 2026-10-03 at 11:30 UTC. */
export const CHAIN_SUCCESS_AT = '2026-10-03T11:30:00Z';
/** The newest price: 2026-10-03 at 11:50 UTC. */
export const PRICES_FETCHED_AT = '2026-10-03T11:50:00Z';

export function timer(overrides: Partial<SchedulerStatus> = {}): SchedulerStatus {
  return {
    name: 'balance-sync',
    state: 'ok',
    last_tick_at: LAST_TICK_AT,
    last_tick_succeeded: true,
    ...overrides,
  };
}

/** The four timers, in the order the backend serves them, all running and well. */
export const okTimers: readonly SchedulerStatus[] = [
  timer({ name: 'balance-sync' }),
  timer({ name: 'price-refresh' }),
  timer({ name: 'price-backfill' }),
  timer({ name: 'backup' }),
];

export function chain(overrides: Partial<ChainHealth> = {}): ChainHealth {
  return {
    chain_key: 'bitcoin',
    state: 'ok',
    last_success_at: CHAIN_SUCCESS_AT,
    last_error_kind: null,
    ...overrides,
  };
}

export const okChains: ChainsHealth = { state: 'ok', items: [chain()] };
export const emptyChains: ChainsHealth = { state: 'ok', items: [] };
export const unavailableChains: ChainsHealth = { state: 'unavailable', items: [] };

export const freshPrices: PricesHealth = { state: 'fresh', latest_fetched_at: PRICES_FETCHED_AT };
export const stalePrices: PricesHealth = { state: 'stale', latest_fetched_at: PRICES_FETCHED_AT };
export const neverPrices: PricesHealth = { state: 'never', latest_fetched_at: null };
export const unavailablePrices: PricesHealth = { state: 'unavailable', latest_fetched_at: null };

/** Everything but `backup`: every section answered and well. */
export type HealthSections = Omit<HealthDetail, 'backup'>;

export const okSections: HealthSections = {
  schedulers: [...okTimers],
  chains: okChains,
  prices: freshPrices,
};
