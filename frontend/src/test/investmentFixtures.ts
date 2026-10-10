/**
 * Fixtures for spec 042: `GET /api/investment` and `/api/exchange-operations`.
 *
 * Amounts are written the way the backend serializes them, at the scale it stores them, so a
 * test that formats one formats what production formats.
 */
import type {
  AssetInvestment,
  Investment,
  Operation,
  OperationList,
  TotalInvestment,
} from '@/api/operations';

/** What the backend answers before anything is tracked or uploaded. */
export function emptyInvestment(overrides: Partial<Investment> = {}): Investment {
  return {
    assets: [],
    overall: {
      invested: '0',
      value: '0',
      pnl: '0',
      pnl_pct: null,
      unavailable: 'nothing_invested',
    },
    invested_by_day: [],
    ...overrides,
  };
}

export function assetInvestment(overrides: Partial<AssetInvestment> = {}): AssetInvestment {
  return {
    asset: 'BTC',
    invested: '13007.000000000000000000',
    value: '24000.000000000000000000',
    pnl: '10993.000000000000000000',
    pnl_pct: '84.5160',
    held: '0.40000000',
    explained: '0.400000000000000000',
    difference: '0.000000000000000000',
    trades: 2,
    unvalued_trades: 0,
    unavailable: null,
    ...overrides,
  };
}

export function totalInvestment(overrides: Partial<TotalInvestment> = {}): TotalInvestment {
  return {
    invested: '13507.000000000000000000',
    value: '24600.000000000000000000',
    pnl: '11093.000000000000000000',
    pnl_pct: '82.1278',
    unavailable: null,
    ...overrides,
  };
}

/** BTC up and KAS down, with KAS holding 100 fewer than its operations explain. */
export const INVESTED: Investment = {
  assets: [
    assetInvestment(),
    assetInvestment({
      asset: 'KAS',
      invested: '700.000000000000000000',
      value: '600.000000000000000000',
      pnl: '-100.000000000000000000',
      pnl_pct: '-14.2857',
      held: '6000.00000000',
      explained: '6100.000000000000000000',
      difference: '-100.000000000000000000',
      trades: 1,
    }),
  ],
  overall: totalInvestment(),
  invested_by_day: [
    { day: '2026-05-01', invested: '20000.000000000000000000' },
    { day: '2026-05-04', invested: '13507.000000000000000000' },
  ],
};

export function operation(overrides: Partial<Operation> = {}): Operation {
  return {
    id: 1,
    source: 'bitget',
    venue: 'Bitget',
    external_id: 'bitget_spot_order_details:abc',
    executed_at: '2026-05-01T12:00:00Z',
    kind: 'buy',
    asset: 'BTC',
    quantity: '0.500000000000000000',
    quote_currency: 'USDT',
    quote_amount: '20000.000000000000000000',
    fee_asset: 'BTC',
    fee_amount: '0.000500000000000000',
    description: 'Spot Buy',
    manual: false,
    ...overrides,
  };
}

export function operationList(operations: readonly Operation[], count?: number): OperationList {
  return { count: count ?? operations.length, operations: [...operations] };
}
