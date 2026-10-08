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
    holdings: [],
    missing: [],
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

/** BTC and KAS held in the wallets, worth 30,770.00 USDT. */
export const VALUED_SUMMARY: PortfolioSummary = portfolioSummary({
  total_value: '30770.000000000000000000',
  holdings: [BTC_HOLDING, KAS_HOLDING],
});
