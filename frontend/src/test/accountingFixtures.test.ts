import { describe, expect, it } from 'vitest';

import {
  breakEvenPortfolio,
  emptySnapshot,
  ethUnpriced,
  everyHeldPositionExcluded,
  failedFirstRecompute,
  investedPortfolio,
  kasLossPortfolio,
  kasUnknownBasis,
  lastRecompute,
  noSnapshot,
  position,
  positionsResponse,
  solUnknownAndUnpriced,
  stablecoinOnlySnapshot,
  tinyPnlPortfolio,
  totals,
  xrpClosed,
  ZERO,
} from './accountingFixtures';

/**
 * The control on the accounting fixture guard. Its silence on every scenario the page tests
 * use means something only if it speaks on the states the backend cannot write.
 */
describe('the accounting fixture guard', () => {
  it('accepts every scenario the page tests use', () => {
    for (const build of [
      investedPortfolio,
      everyHeldPositionExcluded,
      kasLossPortfolio,
      breakEvenPortfolio,
      tinyPnlPortfolio,
      emptySnapshot,
      stablecoinOnlySnapshot,
      noSnapshot,
      failedFirstRecompute,
    ]) {
      expect(build).not.toThrow();
    }
  });

  it('derives the exclusions, a position that is both unknown-basis and unpriced once', () => {
    expect(investedPortfolio().totals.excluded).toEqual([
      { asset: 'ETH', reason: 'unpriced' },
      { asset: 'KAS', reason: 'unknown_basis' },
      { asset: 'SOL', reason: 'unknown_basis' },
    ]);
  });

  it('refuses a market value that is not the price times the quantity', () => {
    expect(() =>
      positionsResponse({ positions: [position({ market_value: '90000.010000000000000000' })] }),
    ).toThrow('BTC market_value is 90000.010000000000000000; it works out to 90000');
  });

  it('refuses a P&L over the unknown-cost part', () => {
    // 1500 x 0.08 - 100 = +20 counts the 500 units of no known cost as pure gain.
    expect(() =>
      positionsResponse({
        positions: [kasUnknownBasis({ unrealized_pnl: '20.000000000000000000' })],
      }),
    ).toThrow('KAS unrealized_pnl');
  });

  it('refuses totals that do not add up', () => {
    expect(() =>
      positionsResponse({
        positions: [position()],
        totals: totals({ total_invested: '52500.000000000000000000' }),
      }),
    ).toThrow('totals.market_value');
  });

  it('refuses stated exclusions the positions do not give', () => {
    expect(() =>
      positionsResponse({
        positions: [solUnknownAndUnpriced()],
        totals: totals({
          excluded: [
            { asset: 'SOL', reason: 'unknown_basis' },
            { asset: 'SOL', reason: 'unpriced' },
          ],
        }),
      }),
    ).toThrow('totals.excluded');
  });

  it('refuses an unknown_basis flag with no unknown-cost units, and the reverse', () => {
    expect(() =>
      positionsResponse({ positions: [position({ flags: ['unknown_basis'] })] }),
    ).toThrow('unknown_basis describes the pool');
    expect(() =>
      positionsResponse({
        positions: [kasUnknownBasis({ flags: [] })],
        totals: totals({ excluded: [] }),
      }),
    ).toThrow('unknown_basis describes the pool');
  });

  it('refuses a missing value without its reason, and a price reason on a priced position', () => {
    expect(() =>
      positionsResponse({ positions: [ethUnpriced({ market_value_unavailable_reason: null })] }),
    ).toThrow('a null market value always has its reason');
    expect(() =>
      positionsResponse({
        positions: [
          position({ market_value: null, market_value_unavailable_reason: 'never_fetched' }),
        ],
      }),
    ).toThrow('a priced position has no price reason');
  });

  it('refuses a figure at the wrong scale', () => {
    // "52500" is the right number and the wrong wire shape: it would make every
    // `<data value>` assertion on it prove nothing about trailing zeros.
    expect(() => positionsResponse({ positions: [position({ total_invested: '52500' })] })).toThrow(
      'has 0 places; the backend sends 18',
    );
  });

  it('refuses positions out of order, and a snapshot-less response with positions', () => {
    expect(() => positionsResponse({ positions: [kasUnknownBasis(), position()] })).toThrow(
      'ordered by asset',
    );
    expect(() =>
      positionsResponse({ computed_at: null, last_recompute: null, positions: [xrpClosed()] }),
    ).toThrow('with no snapshot');
  });

  it('refuses a recompute outcome that disagrees with its error', () => {
    expect(() =>
      positionsResponse({ last_recompute: lastRecompute({ outcome: 'failed', error: null }) }),
    ).toThrow('a failed recompute records its error class');
    expect(() => positionsResponse({ computed_at: null, last_recompute: lastRecompute() })).toThrow(
      'left a snapshot behind it',
    );
  });

  it('refuses a closed position that still claims a price-free value', () => {
    expect(() =>
      positionsResponse({
        positions: [
          xrpClosed({ market_value: null, market_value_unavailable_reason: 'never_fetched' }),
        ],
      }),
    ).toThrow('XRP market_value is null');
    expect(xrpClosed().market_value).toBe(ZERO);
  });
});
