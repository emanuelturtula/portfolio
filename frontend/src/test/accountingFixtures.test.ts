import { describe, expect, it } from 'vitest';

import {
  bgbFeeNeverHeld,
  breakEvenPortfolio,
  emptySnapshot,
  ethUnpriced,
  everyHeldPositionExcluded,
  failedFirstRecompute,
  FEE_OCCURRED_AT,
  feeInNeverHeldAsset,
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
  warning,
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

  it('holds the fee pool the engine opens for a fee paid in an asset never held', () => {
    // A SOL buy's fee paid in BGB: a closed, history_incomplete BGB position, a shortfall and
    // an unvalued fee at the same moment, and the fee's flag on SOL.
    const response = investedPortfolio();
    const bgb = response.positions.find((entry) => entry.asset === 'BGB');

    expect(bgb?.flags).toEqual(['history_incomplete']);
    expect(bgb?.quantity).toBe(ZERO);
    expect(response.warnings.map((entry) => [entry.kind, entry.asset, entry.charged_to])).toEqual([
      ['negative_inventory', 'ETH', null],
      ['negative_inventory', 'BGB', null],
      ['unattributed_fee', 'BGB', 'SOL'],
    ]);
    expect(response.warnings.slice(1).map((entry) => entry.occurred_at)).toEqual([
      FEE_OCCURRED_AT,
      FEE_OCCURRED_AT,
    ]);
  });

  it('refuses a warning on an asset with no position', () => {
    // The fee leg opened a BGB pool: dropping it is the shape spec 022's review caught (S4).
    expect(() =>
      positionsResponse({
        positions: [position({ flags: ['unattributed_fee'] })],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7500.000000000000000000',
        }),
        warnings: [warning({ kind: 'unattributed_fee', asset: 'BGB', charged_to: 'BTC' })],
      }),
    ).toThrow('opened a BGB pool, so it has a position');
  });

  it('refuses a shortfall on an asset not flagged history_incomplete', () => {
    expect(() =>
      positionsResponse({
        positions: [ethUnpriced({ flags: [] })],
        totals: totals({ realized_pnl: '-250.000000000000000000' }),
        warnings: [warning()],
      }),
    ).toThrow('the shortfall sets history_incomplete on ETH');
  });

  it('refuses an unvalued fee whose charged_to is not flagged', () => {
    expect(() =>
      positionsResponse({
        positions: [bgbFeeNeverHeld(), solUnknownAndUnpriced({ flags: ['unknown_basis'] })],
        warnings: feeInNeverHeldAsset({ charged_to: 'SOL' }),
      }),
    ).toThrow('the fee sets unattributed_fee on SOL');
  });

  it('refuses a flag with no warning behind it, either flag', () => {
    expect(() =>
      positionsResponse({
        positions: [ethUnpriced()],
        totals: totals({ realized_pnl: '-250.000000000000000000' }),
      }),
    ).toThrow('ETH: history_incomplete is set only with a negative_inventory warning');
    expect(() =>
      positionsResponse({
        positions: [bgbFeeNeverHeld(), solUnknownAndUnpriced()],
        warnings: feeInNeverHeldAsset({ charged_to: null }),
      }),
    ).toThrow('SOL: unattributed_fee is set only with a warning charged to it');
  });

  it('accepts an unvalued fee on a conversion between stablecoins, charged to nothing', () => {
    expect(() =>
      positionsResponse({
        positions: [bgbFeeNeverHeld()],
        warnings: feeInNeverHeldAsset({ charged_to: null }),
      }),
    ).not.toThrow();
  });

  it('refuses warnings out of event order, and a warning of nothing', () => {
    expect(() =>
      positionsResponse({
        positions: [bgbFeeNeverHeld(), ethUnpriced()],
        totals: totals({ realized_pnl: '-250.000000000000000000' }),
        warnings: [...feeInNeverHeldAsset({ charged_to: null }), warning()],
      }),
    ).toThrow('warnings are stored in event order');
    expect(() =>
      positionsResponse({
        positions: [ethUnpriced()],
        totals: totals({ realized_pnl: '-250.000000000000000000' }),
        warnings: [warning({ quantity: ZERO })],
      }),
    ).toThrow('a positive quantity');
  });
});
