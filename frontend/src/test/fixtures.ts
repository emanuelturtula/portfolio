import type { components } from '@/api/generated/schema';

/**
 * Fixture builders for the wallet and balance endpoints.
 *
 * Every builder is typed with the generated OpenAPI types, so a field the
 * backend adds, renames or makes nullable breaks a fixture at compile time
 * rather than leaving the tests asserting against a shape the backend no
 * longer sends.
 *
 * Every number that is money is a string, exactly as it crosses the wire, and
 * every expected sum written in a test is worked out by hand in a comment
 * rather than computed with the code under test.
 */

type Schemas = components['schemas'];

export type WalletResponse = Schemas['WalletResponse'];
export type WalletBalanceResponse = Schemas['WalletBalanceResponse'];
export type CurrentBalancesResponse = Schemas['CurrentBalancesResponse'];
export type PriceResponse = Schemas['PriceResponse'];
export type UnpricedHoldingResponse = Schemas['UnpricedHoldingResponse'];
export type UnreadWalletResponse = Schemas['UnreadWalletResponse'];
export type SyncRunResponse = Schemas['SyncRunResponse'];
export type SyncTriggeredResponse = Schemas['SyncTriggeredResponse'];
export type ChainOutcomeResponse = Schemas['ChainOutcomeResponse'];
export type SyncErrorKind = Schemas['SyncErrorKind'];
export type PriceUnavailable = Schemas['PriceUnavailable'];
export type ChainKey = Schemas['ChainKey'];

/**
 * Testnet addresses only. Each one is a checksum-valid vector the backend's own
 * address tests already use (`backend/tests/address_vectors.py`), so none of
 * them is invented and none of them is mainnet. The gitleaks rules reject the
 * mainnet shapes (`bc1`, `1`, `3`, `kaspa:`), which is the second line of
 * defence behind this comment.
 */
export const ADDRESSES = {
  /** BIP173 testnet P2WPKH. */
  btcSegwit: 'tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx',
  /** Bitcoin Core regtest P2WPKH. */
  btcRegtest: 'bcrt1qdavt4j2sd7dlhqsavtnfxvzppw6k7qy97tmnu9',
  /** Bitcoin Core testnet4 P2PKH (Base58, starts with `m`). */
  btcLegacy: 'mwgS2HRbjyfYxFnR1nF9VKLvmdgMfFBmGq',
  /** Bitcoin Core testnet4 P2SH (Base58, starts with `2`). */
  btcScript: '2MwBVrJQ76BdaGD76CTmou8cZzQYLpe4NqU',
  /** Kaspa testnet, version 1, a real key. */
  kasPrimary: 'kaspatest:qxaqrlzlf6wes72en3568khahq66wf27tuhfxn5nytkd8tcep2c0vrse6gdmpks',
  /** Kaspa testnet, version 0. */
  kasSecondary: 'kaspatest:qqnapngv3zxp305qf06w6hpzmyxtx2r99jjhs04lu980xdyd2ulwwmx9evrfz',
} as const;

/** Every fixture address, for the privacy test that no request URL carries one. */
export const ALL_ADDRESSES: readonly string[] = Object.values(ADDRESSES);

/**
 * The fixed clock every dashboard test runs under.
 *
 * The timestamps below sit on whole minutes on purpose: a relative time of
 * "18 minutes ago" is the same under a formatter that floors and one that
 * rounds, so the tests pin what the page says rather than which rounding mode
 * a helper happened to choose.
 */
export const NOW = '2026-09-24T12:00:00.000Z';

/** The latest settled run: started 20 minutes before {@link NOW}, finished 15 before. */
export const RUN_STARTED_AT = '2026-09-24T11:40:00.000Z';
export const RUN_FINISHED_AT = '2026-09-24T11:45:00.000Z';

/** Bitcoin was read the instant the run started; "at or after" includes equality. */
export const BTC_OBSERVED_AT = '2026-09-24T11:40:00.000Z';
/** Kaspa was read two minutes in, so `as_of` is 18 minutes before {@link NOW}. */
export const KAS_OBSERVED_AT = '2026-09-24T11:42:00.000Z';

/** The run before that one: started 35 minutes before {@link NOW}. */
export const PREVIOUS_RUN_STARTED_AT = '2026-09-24T11:25:00.000Z';
export const PREVIOUS_RUN_FINISHED_AT = '2026-09-24T11:26:00.000Z';
/** A reading taken by the previous run, which the latest run did not refresh. */
export const PREVIOUS_OBSERVED_AT = '2026-09-24T11:25:00.000Z';

/** When the prices were refreshed. 22 minutes before {@link NOW}. */
export const PRICE_AS_OF = '2026-09-24T11:38:00.000Z';
/** A price that is two hours old and that the backend has flagged `stale`. */
export const STALE_PRICE_AS_OF = '2026-09-24T10:00:00.000Z';

/** A timestamp for the rows `GET /api/wallets` returns. */
export const CREATED_AT = '2026-09-20T09:00:00.000Z';

const ASSET_OF: Readonly<Record<ChainKey, string>> = { bitcoin: 'BTC', kaspa: 'KAS' };

/** The asset symbol the backend derives from a chain key. */
export function assetOf(chainKey: ChainKey): string {
  return ASSET_OF[chainKey];
}

export function wallet(overrides: Partial<WalletResponse> = {}): WalletResponse {
  return {
    id: 1,
    chain_key: 'bitcoin',
    address: ADDRESSES.btcSegwit,
    label: 'Cold storage',
    archived: false,
    created_at: CREATED_AT,
    updated_at: CREATED_AT,
    ...overrides,
  };
}

export function price(overrides: Partial<PriceResponse> = {}): PriceResponse {
  return {
    amount: '52000.00',
    source: 'kraken',
    as_of: PRICE_AS_OF,
    stale: false,
    ...overrides,
  };
}

export function walletBalance(
  overrides: Partial<WalletBalanceResponse> = {},
): WalletBalanceResponse {
  return {
    wallet_id: 1,
    chain_key: 'bitcoin',
    label: 'Cold storage',
    asset_symbol: 'BTC',
    confirmed: '150000000',
    pending: null,
    decimals: 8,
    quantity: '1.50000000',
    // 1.50000000 x 52000.00, at the 8 + 2 places Decimal multiplication keeps.
    value: '78000.0000000000',
    price: price(),
    observed_at: BTC_OBSERVED_AT,
    ...overrides,
  };
}

/**
 * The row the backend sends for a wallet no successful run has read:
 * `confirmed`, `quantity`, `value` and `observed_at` all null. Never zero.
 *
 * `price` is whatever the asset's price is, because the backend fills it in
 * per asset: an unread wallet of an asset some other wallet holds still gets a
 * price, and still has no value.
 */
export function unreadBalance(
  source: Pick<WalletResponse, 'id' | 'chain_key' | 'label'>,
  assetPrice: PriceResponse | null = null,
): WalletBalanceResponse {
  const chainKey = source.chain_key as ChainKey;

  return {
    wallet_id: source.id,
    chain_key: source.chain_key,
    label: source.label,
    asset_symbol: assetOf(chainKey),
    confirmed: null,
    pending: null,
    decimals: null,
    quantity: null,
    value: null,
    price: assetPrice,
    observed_at: null,
  };
}

export function unreadEntry(
  source: Pick<WalletResponse, 'id' | 'chain_key'>,
): UnreadWalletResponse {
  return {
    wallet_id: source.id,
    chain_key: source.chain_key,
    asset_symbol: assetOf(source.chain_key as ChainKey),
  };
}

export function currentBalances(
  overrides: Partial<CurrentBalancesResponse> = {},
): CurrentBalancesResponse {
  return {
    quote_currency: 'EUR',
    total: '0',
    complete: true,
    as_of: null,
    wallets: [],
    unpriced: [],
    unread: [],
    ...overrides,
  };
}

export function chainOutcome(overrides: Partial<ChainOutcomeResponse> = {}): ChainOutcomeResponse {
  return {
    chain_key: 'bitcoin',
    status: 'success',
    wallets_read: 1,
    error_kind: null,
    detail: null,
    ...overrides,
  };
}

/** A chain outcome that failed, with the detail a provider would write. */
export function failedOutcome(
  chainKey: ChainKey,
  errorKind: SyncErrorKind,
  detail: string | null = 'The provider did not answer.',
): ChainOutcomeResponse {
  return chainOutcome({
    chain_key: chainKey,
    status: 'failed',
    wallets_read: 0,
    error_kind: errorKind,
    detail,
  });
}

export function syncRun(overrides: Partial<SyncRunResponse> = {}): SyncRunResponse {
  return {
    run_id: 7,
    trigger: 'scheduled',
    status: 'success',
    started_at: RUN_STARTED_AT,
    finished_at: RUN_FINISHED_AT,
    duration_ms: 300_000,
    wallets_total: 3,
    wallets_succeeded: 3,
    wallets_failed: 0,
    chains: [
      chainOutcome({ chain_key: 'bitcoin', wallets_read: 2 }),
      chainOutcome({ chain_key: 'kaspa', wallets_read: 1 }),
    ],
    ...overrides,
  };
}

/** A run that is still in flight: no `finished_at`, no duration, no outcomes yet. */
export function runningRun(overrides: Partial<SyncRunResponse> = {}): SyncRunResponse {
  return syncRun({
    run_id: 8,
    status: 'running',
    started_at: '2026-09-24T11:59:00.000Z',
    finished_at: null,
    duration_ms: null,
    wallets_succeeded: 0,
    chains: [],
    ...overrides,
  });
}

/** A run whose process died: no `finished_at`, and only the chains it got to. */
export function interruptedRun(overrides: Partial<SyncRunResponse> = {}): SyncRunResponse {
  return syncRun({
    status: 'interrupted',
    finished_at: null,
    duration_ms: null,
    wallets_succeeded: 2,
    chains: [chainOutcome({ chain_key: 'bitcoin', wallets_read: 2 })],
    ...overrides,
  });
}

export function previousRun(overrides: Partial<SyncRunResponse> = {}): SyncRunResponse {
  return syncRun({
    run_id: 6,
    started_at: PREVIOUS_RUN_STARTED_AT,
    finished_at: PREVIOUS_RUN_FINISHED_AT,
    duration_ms: 60_000,
    ...overrides,
  });
}

export function triggered(
  run: SyncRunResponse = syncRun({ trigger: 'manual' }),
  joined = false,
): SyncTriggeredResponse {
  return { ...run, joined };
}

/**
 * A base-unit balance past `Number.MAX_SAFE_INTEGER` (9007199254740991).
 *
 * 2,870,000,000,000,000,123 sompi is 28,700,000,000.00000123 KAS, about the
 * whole supply. As a JavaScript number it becomes 2870000000000000000: the
 * trailing 123 is gone, silently, which is exactly what this fixture exists to
 * catch.
 */
export const HUGE_KAS_CONFIRMED = '2870000000000000123';
export const HUGE_KAS_QUANTITY = '28700000000.00000123';

/**
 * The healthy portfolio every dashboard test starts from: two Bitcoin wallets
 * and one Kaspa wallet, all read by the latest run, all priced, nothing stale.
 *
 * Hand-worked values, so the tests never compute an expectation with the code
 * they are testing:
 *
 * | Wallet | Quantity | Price | Value |
 * |---|---|---|---|
 * | 1 BTC "Cold storage" | 1.50000000 | 52000.00 | 78000.0000000000 |
 * | 2 BTC "Spending" | 0.12345678 | 52000.00 | 6419.7525600000 |
 * | 3 KAS (no label) | 28700000000.00000123 | 0.08 | 2296000000.0000000984 |
 *
 * - BTC quantity: 1.50000000 + 0.12345678 = 1.62345678
 * - BTC value: 78000 + 6419.75256 = 84419.75256
 * - Total: 84419.75256 + 2296000000.0000000984 = 2296084419.7525600984
 */
export const HEALTHY = {
  btcQuantitySum: '1.62345678',
  btcValueSum: '84419.75256',
  kasValue: '2296000000.0000000984',
  total: '2296084419.7525600984',
} as const;

export const KAS_PRICE = '0.08';

export interface PortfolioScenario {
  readonly wallets: WalletResponse[];
  readonly current: CurrentBalancesResponse;
  readonly runs: SyncRunResponse[];
}

export function healthyPortfolio(): PortfolioScenario {
  const btcPrice = price();
  const kasPrice = price({ amount: KAS_PRICE, source: 'kaspa' });

  return {
    wallets: [
      wallet({ id: 1, chain_key: 'bitcoin', address: ADDRESSES.btcSegwit, label: 'Cold storage' }),
      wallet({ id: 2, chain_key: 'bitcoin', address: ADDRESSES.btcLegacy, label: 'Spending' }),
      wallet({ id: 3, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: null }),
    ],
    current: currentBalances({
      total: HEALTHY.total,
      complete: true,
      as_of: KAS_OBSERVED_AT,
      wallets: [
        walletBalance({ wallet_id: 1, label: 'Cold storage', price: btcPrice }),
        walletBalance({
          wallet_id: 2,
          label: 'Spending',
          confirmed: '12345678',
          quantity: '0.12345678',
          value: '6419.7525600000',
          price: btcPrice,
        }),
        walletBalance({
          wallet_id: 3,
          chain_key: 'kaspa',
          label: null,
          asset_symbol: 'KAS',
          confirmed: HUGE_KAS_CONFIRMED,
          quantity: HUGE_KAS_QUANTITY,
          value: HEALTHY.kasValue,
          price: kasPrice,
          observed_at: KAS_OBSERVED_AT,
        }),
      ],
    }),
    runs: [syncRun(), previousRun()],
  };
}

/**
 * One chain down: the latest run read Bitcoin and failed on Kaspa, so the
 * Kaspa row still carries the reading the previous run took.
 *
 * The total still includes that Kaspa value - the backend sums every row that
 * has one - which is why the page has to say the total includes a balance the
 * last sync could not refresh.
 */
export function kaspaDownPortfolio(errorKind: SyncErrorKind = 'unavailable'): PortfolioScenario {
  const healthy = healthyPortfolio();

  return {
    wallets: healthy.wallets,
    current: {
      ...healthy.current,
      as_of: BTC_OBSERVED_AT,
      wallets: healthy.current.wallets.map((row) =>
        row.chain_key === 'kaspa' ? { ...row, observed_at: PREVIOUS_OBSERVED_AT } : row,
      ),
    },
    runs: [
      syncRun({
        status: 'partial',
        wallets_succeeded: 2,
        wallets_failed: 1,
        chains: [
          chainOutcome({ chain_key: 'bitcoin', wallets_read: 2 }),
          failedOutcome('kaspa', errorKind),
        ],
      }),
      previousRun(),
    ],
  };
}

/** No wallets registered at all: the first-time user. */
export function emptyPortfolio(): PortfolioScenario {
  return { wallets: [], current: currentBalances(), runs: [] };
}
