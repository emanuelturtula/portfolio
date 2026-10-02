import Decimal from 'decimal.js';
import { describe, expect, it } from 'vitest';

import {
  bgbFeeNeverHeld,
  breakEvenPortfolio,
  cancellingUnmatchedProceeds,
  emptySnapshot,
  ethSoldAtUnknownCost,
  ethUnpriced,
  everyHeldPositionExcluded,
  everyHeldPositionExcludedWithUnmatched,
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
  UNMATCHED,
  unmatchedProceedsPortfolio,
  warning,
  xrpClosed,
  ZERO,
  type PositionsResponse,
} from './accountingFixtures';

/** Every scenario the page tests use: the guard is silent on each, and that is checked. */
const SCENARIOS: readonly (() => PositionsResponse)[] = [
  investedPortfolio,
  everyHeldPositionExcluded,
  kasLossPortfolio,
  breakEvenPortfolio,
  tinyPnlPortfolio,
  unmatchedProceedsPortfolio,
  cancellingUnmatchedProceeds,
  everyHeldPositionExcludedWithUnmatched,
  emptySnapshot,
  stablecoinOnlySnapshot,
  noSnapshot,
  failedFirstRecompute,
];

/**
 * A `decimal.js` of this file's own, wide enough to add 18-place amounts exactly: the default
 * precision of 20 would round the 23 digits of a total like 21494.417890123456789012.
 */
const Exact = Decimal.clone({ precision: 60 });

/**
 * The control on the accounting fixture guard. Its silence on every scenario the page tests
 * use means something only if it speaks on the states the backend cannot write.
 */
describe('the accounting fixture guard', () => {
  it('accepts every scenario the page tests use', () => {
    for (const build of SCENARIOS) {
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

  it("holds every scenario's unmatched total to the sum of its positions' own (spec 026)", () => {
    // Summed here, apart from the guard, so the guard is not the only thing that says so.
    for (const build of SCENARIOS) {
      const response = build();
      const sum = response.positions.reduce(
        (total, entry) => total.plus(entry.unmatched_proceeds),
        new Exact(0),
      );
      expect(new Exact(response.totals.unmatched_proceeds).eq(sum)).toBe(true);
    }
    // The control: the scenarios do carry figures, a negative one and a pair that cancels.
    expect(unmatchedProceedsPortfolio().totals.unmatched_proceeds).toBe(UNMATCHED.total);
    expect(UNMATCHED.total).toBe('21494.417890123456789012');
    expect(unmatchedProceedsPortfolio().positions.map((entry) => entry.unmatched_proceeds)).toEqual(
      [ZERO, UNMATCHED.btc, UNMATCHED.eth, UNMATCHED.kas, UNMATCHED.xrp],
    );
    expect(cancellingUnmatchedProceeds().totals.unmatched_proceeds).toBe(ZERO);
    expect(
      cancellingUnmatchedProceeds().positions.map((entry) => entry.unmatched_proceeds),
    ).toEqual([ZERO, '-50.250000000000000000', '50.250000000000000000']);
  });

  it('refuses an unmatched total of zero while a position carries a figure', () => {
    expect(() => positionsResponse({ positions: [ethSoldAtUnknownCost()] })).toThrow(
      'totals.unmatched_proceeds is 0.000000000000000000; it works out to 1234.567890123456789012',
    );
  });

  it('refuses an unmatched total no position stands behind', () => {
    // The response the page has no branch for: spec 026 calls it one the backend cannot write.
    expect(() =>
      positionsResponse({
        positions: [xrpClosed()],
        totals: totals({
          realized_pnl: '125.500000000000000000',
          unmatched_proceeds: '10.000000000000000000',
        }),
      }),
    ).toThrow('totals.unmatched_proceeds is 10.000000000000000000; it works out to 0.0');
  });

  it('refuses an unmatched total that leaves out a position left out of the other totals', () => {
    // 21494.417890123456789012 without KAS's -50.25 is 21544.667890123456789012: the sum over
    // the counted positions only, which is how the other totals are summed and this one is not.
    const stated = unmatchedProceedsPortfolio().totals;
    expect(() =>
      unmatchedProceedsPortfolio({
        totals: { ...stated, unmatched_proceeds: '21544.667890123456789012' },
      }),
    ).toThrow(
      `totals.unmatched_proceeds is 21544.667890123456789012; it works out to ${UNMATCHED.total}`,
    );
  });

  it('refuses an unmatched total that leaves out the positions no longer held', () => {
    // BTC's 20000 and KAS's -50.25 alone: 19949.75, the sum over the held positions.
    const stated = unmatchedProceedsPortfolio().totals;
    expect(() =>
      unmatchedProceedsPortfolio({
        totals: { ...stated, unmatched_proceeds: '19949.750000000000000000' },
      }),
    ).toThrow('totals.unmatched_proceeds');
  });

  it('refuses an unmatched total that is off by one unit in the last place', () => {
    const stated = unmatchedProceedsPortfolio().totals;
    expect(() =>
      unmatchedProceedsPortfolio({
        totals: { ...stated, unmatched_proceeds: '21494.417890123456789013' },
      }),
    ).toThrow('totals.unmatched_proceeds');
  });

  it('refuses an unmatched figure at the wrong scale, on a position or in the totals', () => {
    expect(() =>
      positionsResponse({ positions: [ethSoldAtUnknownCost({ unmatched_proceeds: '1234.5' })] }),
    ).toThrow('ETH unmatched_proceeds "1234.5" has 1 places; the backend sends 18');
    expect(() => positionsResponse({ totals: totals({ unmatched_proceeds: '0' }) })).toThrow(
      'totals.unmatched_proceeds "0" has 0 places; the backend sends 18',
    );
  });

  it('accepts a closed position that carries unmatched proceeds and no flag', () => {
    // The sharpest case of spec 026: `unknown_basis` is not sticky, so nothing else marks it.
    const eth = ethSoldAtUnknownCost();

    expect(eth.flags).toEqual([]);
    expect(eth.quantity).toBe(ZERO);
    expect(eth.realized_pnl).toBe(ZERO);
    expect(() =>
      positionsResponse({
        positions: [eth],
        totals: totals({ unmatched_proceeds: eth.unmatched_proceeds }),
      }),
    ).not.toThrow();
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
