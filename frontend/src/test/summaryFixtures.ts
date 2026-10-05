/**
 * Fixtures for `GET /api/portfolio/summary` (#154).
 *
 * Amounts are written the way the backend serializes them: fixed point, at the scale the
 * domain rounds to (18 places for a value, 4 for a percentage), so a test that formats one
 * formats what production formats.
 */
import type { Holding, MissingPiece, PortfolioSummary } from '@/api/portfolio';

const ZERO_VALUE = '0.000000000000000000';

/** The summary of an install with nothing in it. */
export function portfolioSummary(overrides: Partial<PortfolioSummary> = {}): PortfolioSummary {
  return {
    total_value: ZERO_VALUE,
    invested: ZERO_VALUE,
    pnl: ZERO_VALUE,
    pnl_pct: null,
    holdings: [],
    missing: [],
    untracked: [],
    ...overrides,
  };
}

/** A holding with no price: in the table, in none of the figures. */
export function unpricedHolding(asset: string, quantity: string): Holding {
  return { asset, quantity, price: null, value: null, share_pct: null };
}

export function missing(kind: MissingPiece['kind'], subject: string): MissingPiece {
  return { kind, subject };
}

export const BTC_HOLDING: Holding = {
  asset: 'BTC',
  quantity: '0.4995',
  price: '60000',
  value: '29970.000000000000000000',
  share_pct: '97.4001',
};

export const KAS_HOLDING: Holding = {
  asset: 'KAS',
  quantity: '8000',
  price: '0.1',
  value: '800.000000000000000000',
  share_pct: '2.5999',
};

/**
 * The backend's own scenario (`backend/tests/api/test_portfolio_summary.py`): BTC and KAS held
 * across a wallet and a venue, 30,701.30 USDT net in, worth 30,770.00.
 */
export const GAINING_SUMMARY: PortfolioSummary = portfolioSummary({
  total_value: '30770.000000000000000000',
  invested: '30701.300000000000000000',
  pnl: '68.700000000000000000',
  pnl_pct: '0.2238',
  holdings: [BTC_HOLDING, KAS_HOLDING],
});

/** The same holdings, bought dearer: a loss. */
export const LOSING_SUMMARY: PortfolioSummary = portfolioSummary({
  total_value: '30770.000000000000000000',
  invested: '35000.000000000000000000',
  pnl: '-4230.000000000000000000',
  pnl_pct: '-12.0857',
  holdings: [BTC_HOLDING, KAS_HOLDING],
});
