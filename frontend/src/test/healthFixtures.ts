import type {
  ChainHealth,
  ChainsHealth,
  ExchangeHealth,
  ExchangesHealth,
  HealthDetail,
  PricesHealth,
  ReconciliationHealth,
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
/** An account's last successful sync: 2026-10-03 at 10:15 UTC. */
export const SYNCED_AT = '2026-10-03T10:15:00Z';
/** An account's last balance read: 2026-10-03 at 10:16 UTC. */
export const BALANCES_READ_AT = '2026-10-03T10:16:00Z';
/** The newest price: 2026-10-03 at 11:50 UTC. */
export const PRICES_FETCHED_AT = '2026-10-03T11:50:00Z';
/** The reconciliation's snapshot: 2026-10-03 at 09:05 UTC. */
export const RECONCILED_AT = '2026-10-03T09:05:00Z';

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
  timer({ name: 'exchange-sync' }),
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

export function exchange(overrides: Partial<ExchangeHealth> = {}): ExchangeHealth {
  return {
    exchange_key: 'bitget',
    sync_state: 'ok',
    last_synced_at: SYNCED_AT,
    balances_state: 'ok',
    balances_read_at: BALANCES_READ_AT,
    ...overrides,
  };
}

export const okExchanges: ExchangesHealth = { state: 'ok', items: [exchange()] };
export const emptyExchanges: ExchangesHealth = { state: 'ok', items: [] };
export const unavailableExchanges: ExchangesHealth = { state: 'unavailable', items: [] };

export const freshPrices: PricesHealth = { state: 'fresh', latest_fetched_at: PRICES_FETCHED_AT };
export const stalePrices: PricesHealth = { state: 'stale', latest_fetched_at: PRICES_FETCHED_AT };
export const neverPrices: PricesHealth = { state: 'never', latest_fetched_at: null };
export const unavailablePrices: PricesHealth = { state: 'unavailable', latest_fetched_at: null };

export function reconciliation(
  overrides: Partial<ReconciliationHealth> = {},
): ReconciliationHealth {
  return {
    state: 'match',
    computed_at: RECONCILED_AT,
    assets_compared: 4,
    assets_mismatched: 0,
    sources_not_compared: 0,
    ...overrides,
  };
}

export const matchReconciliation = reconciliation();
export const notComputedReconciliation = reconciliation({
  state: 'not_computed',
  computed_at: null,
  assets_compared: 0,
  assets_mismatched: 0,
  sources_not_compared: 0,
});
export const unavailableReconciliation = reconciliation({
  state: 'unavailable',
  computed_at: null,
  assets_compared: null,
  assets_mismatched: null,
  sources_not_compared: null,
});

/** Everything but `backup`: every section answered and well. */
export type HealthSections = Omit<HealthDetail, 'backup'>;

export const okSections: HealthSections = {
  schedulers: [...okTimers],
  chains: okChains,
  exchanges: okExchanges,
  prices: freshPrices,
  reconciliation: matchReconciliation,
};
