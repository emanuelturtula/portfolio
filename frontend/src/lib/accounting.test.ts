import { describe, expect, it } from 'vitest';

import {
  assetsWithUnreliableRealizedPnl,
  describeClosedPositions,
  describeEmptyPositions,
  EXCLUSION_REASON_MESSAGES,
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  flagsOf,
  formatVenues,
  groupExclusions,
  hasNoKnownCost,
  isHeld,
  MARKET_VALUE_UNAVAILABLE_MESSAGES,
  venueLabel,
  venuesWithFailedSync,
  type EmptyPositions,
} from '@/lib/accounting';
import { PRICE_UNAVAILABLE_MESSAGES } from '@/lib/prices';
import {
  ALL_EXCLUSION_REASONS,
  ALL_MARKET_VALUE_UNAVAILABLE,
  ALL_POSITION_FLAGS,
  ALL_PRICE_UNAVAILABLE,
  bgbFeeNeverHeld,
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
  investedPortfolio,
  noSnapshot,
  position,
  RECOMPUTE_ERROR,
  RECOMPUTE_FAILED_AT,
  stablecoinOnlySnapshot,
  xrpClosed,
  type PositionsResponse,
} from '@/test/accountingFixtures';
import {
  authFailedExchange,
  erroredExchange,
  exchange,
  unsyncedExchange,
  type ExchangeResponse,
} from '@/test/exchangeFixtures';

describe('the flag words', () => {
  it('gives each flag the badge text the spec fixes', () => {
    // Written out from spec 022's flag table rather than imported: a changed badge is a
    // changed contract with the owner, and should be a diff here.
    expect(FLAG_BADGES).toEqual({
      unknown_basis: 'Unknown cost',
      history_incomplete: 'History incomplete',
      unattributed_fee: 'Fee not valued',
    });
  });

  it.each(ALL_POSITION_FLAGS)('explains %s in a sentence of its own', (flag) => {
    const explanation = FLAG_EXPLANATIONS[flag];

    expect(explanation).toMatch(/^[A-Z].*\.$/);
    // An explanation is not the badge again.
    expect(explanation).not.toBe(FLAG_BADGES[flag]);
  });

  it('says what each flag does to the figures', () => {
    expect(FLAG_EXPLANATIONS.unknown_basis).toMatch(/left out of the portfolio totals/);
    expect(FLAG_EXPLANATIONS.history_incomplete).toMatch(/missing/);
    expect(FLAG_EXPLANATIONS.history_incomplete).toMatch(/realized P&L/);
    expect(FLAG_EXPLANATIONS.unattributed_fee).toMatch(/fee/);
  });

  it('says where units of unknown cost come from, and what their market value covers (N2)', () => {
    // The engine writes them from a swap paid with unknown-cost units and from a non-cash fee
    // rebate. A buy before the history begins is not one: selling it is history_incomplete.
    const explanation = FLAG_EXPLANATIONS.unknown_basis;
    expect(explanation).toMatch(/without a known cost/);
    expect(explanation).toMatch(/swap/);
    expect(explanation).toMatch(/rebate/);
    expect(explanation).toMatch(/market value covers every unit/);
    expect(explanation).not.toMatch(/deposit/i);
    expect(explanation).not.toMatch(/bought before/);
  });

  it('says a fee paid in an asset can leave its history short, not only a sale (R6)', () => {
    expect(FLAG_EXPLANATIONS.history_incomplete).toMatch(/fee paid in this asset/);
  });

  it.each(ALL_EXCLUSION_REASONS)('gives the %s exclusion a reason', (reason) => {
    expect(EXCLUSION_REASON_MESSAGES[reason]).toMatch(/\.$/);
  });

  it('gives the two exclusions different reasons', () => {
    expect(EXCLUSION_REASON_MESSAGES.unknown_basis).toMatch(/no known cost/);
    // Not "no price": a value out of range has a price and no market value (N3).
    expect(EXCLUSION_REASON_MESSAGES.unpriced).toMatch(/no market value/);
    expect(EXCLUSION_REASON_MESSAGES.unpriced).not.toMatch(/no price/);
  });
});

describe('the market value sentences', () => {
  it.each(ALL_MARKET_VALUE_UNAVAILABLE)('has a sentence for %s', (reason) => {
    expect(MARKET_VALUE_UNAVAILABLE_MESSAGES[reason]).toMatch(/^[A-Z].*\.$/);
  });

  it.each(ALL_PRICE_UNAVAILABLE)('reuses the value section sentence for %s', (reason) => {
    // One sentence per reason across the dashboard, so the two sections cannot drift.
    expect(MARKET_VALUE_UNAVAILABLE_MESSAGES[reason]).toBe(PRICE_UNAVAILABLE_MESSAGES[reason]);
  });

  it('adds a sentence of its own for a value too large to show', () => {
    expect(MARKET_VALUE_UNAVAILABLE_MESSAGES.value_out_of_range).not.toBe('');
    expect(Object.values(PRICE_UNAVAILABLE_MESSAGES)).not.toContain(
      MARKET_VALUE_UNAVAILABLE_MESSAGES.value_out_of_range,
    );
  });
});

describe('isHeld', () => {
  it('calls a position with nothing left not held, as the wire spells it', () => {
    // The wire sends "0.000000000000000000", which is not the string "0".
    expect(isHeld(xrpClosed())).toBe(false);
    expect(isHeld(xrpClosed({ quantity: '0' }))).toBe(false);
  });

  it('calls the smallest amount the engine carries held', () => {
    expect(isHeld(position({ quantity: '0.000000000000000001' }))).toBe(true);
    expect(isHeld(position())).toBe(true);
  });
});

describe('hasNoKnownCost', () => {
  it('is true when every unit held has an unknown cost, compared as decimals', () => {
    const allUnknown = position({
      quantity: '10.000000000000000000',
      unknown_basis_quantity: '10.000000000000000000',
    });
    expect(hasNoKnownCost(allUnknown)).toBe(true);
    // The same amount spelled two ways is still the same amount.
    expect(hasNoKnownCost({ ...allUnknown, unknown_basis_quantity: '10' })).toBe(true);
  });

  it('is false when any unit has a known cost', () => {
    expect(hasNoKnownCost(position())).toBe(false);
    expect(
      hasNoKnownCost(
        position({
          quantity: '10.000000000000000000',
          unknown_basis_quantity: '9.999999999999999999',
        }),
      ),
    ).toBe(false);
  });
});

describe('flagsOf', () => {
  it('collects each flag once, alphabetically, whichever position carries it first', () => {
    expect(
      flagsOf([
        position({ flags: ['unknown_basis'] }),
        position({ flags: ['history_incomplete', 'unknown_basis'] }),
        position(),
      ]),
    ).toEqual(['history_incomplete', 'unknown_basis']);
    expect(flagsOf([])).toEqual([]);
  });
});

describe('assetsWithUnreliableRealizedPnl', () => {
  it('names every asset, held or not, whose history is short or whose fee went unvalued', () => {
    // BGB is closed and history_incomplete; ETH is held and history_incomplete; SOL was
    // charged an unvalued fee. KAS's unknown basis does not make realized P&L wrong: sales of
    // unknown-cost units are kept out of it.
    expect(assetsWithUnreliableRealizedPnl(investedPortfolio().positions)).toEqual([
      'BGB',
      'ETH',
      'SOL',
    ]);
  });

  it('names nothing when no position carries either flag', () => {
    expect(assetsWithUnreliableRealizedPnl([position(), xrpClosed()])).toEqual([]);
    expect(assetsWithUnreliableRealizedPnl([position({ flags: ['unknown_basis'] })])).toEqual([]);
  });
});

describe('groupExclusions', () => {
  it('groups the assets by reason, in the order each reason first appears', () => {
    expect(
      groupExclusions([
        { asset: 'ETH', reason: 'unpriced' },
        { asset: 'KAS', reason: 'unknown_basis' },
        { asset: 'SOL', reason: 'unknown_basis' },
        { asset: 'XRP', reason: 'unpriced' },
      ]),
    ).toEqual([
      { reason: 'unpriced', assets: ['ETH', 'XRP'] },
      { reason: 'unknown_basis', assets: ['KAS', 'SOL'] },
    ]);
  });

  it('groups nothing into nothing', () => {
    expect(groupExclusions([])).toEqual([]);
  });
});

describe('describeClosedPositions', () => {
  it('names one asset no longer held in the singular', () => {
    expect(describeClosedPositions([xrpClosed()])).toBe(
      '1 asset no longer held is not listed: XRP. Its realized P&L is in the total.',
    );
  });

  it('names several in the plural, in the order given, with the flags beside the flagged ones', () => {
    // M1: history_incomplete is sticky, so the asset with the worst history is often the one
    // with no row. Its flag is named here or nowhere.
    expect(describeClosedPositions([bgbFeeNeverHeld(), xrpClosed()])).toBe(
      '2 assets no longer held are not listed: BGB (History incomplete), XRP. ' +
        'Their realized P&L is in the total.',
    );
  });

  it('names every flag a closed asset carries, by its badge text', () => {
    expect(
      describeClosedPositions([xrpClosed({ flags: ['history_incomplete', 'unattributed_fee'] })]),
    ).toBe(
      '1 asset no longer held is not listed: XRP (History incomplete, Fee not valued). ' +
        'Its realized P&L is in the total.',
    );
  });
});

describe('venue names', () => {
  it('names venues as a person would list them', () => {
    expect(formatVenues(['bingx'])).toBe('BingX');
    expect(formatVenues(['bingx', 'bitget'])).toBe('BingX and Bitget');
  });

  it('names a warning venue by its display name, or by its raw source when unknown', () => {
    expect(venueLabel('bitget')).toBe('Bitget');
    expect(venueLabel('bingx')).toBe('BingX');
    expect(venueLabel('kraken')).toBe('kraken');
    // An inherited property is not a venue: `EXCHANGES.toString` exists on every object.
    expect(venueLabel('toString')).toBe('toString');
    expect(venueLabel('constructor')).toBe('constructor');
  });

  it('lists the venues whose last sync failed, and no other', () => {
    const exchanges: ExchangeResponse[] = [
      authFailedExchange('auth', { exchange_key: 'bingx' }),
      erroredExchange('unavailable'),
    ];

    expect(venuesWithFailedSync(exchanges)).toEqual(['bingx', 'bitget']);
    expect(
      venuesWithFailedSync([unsyncedExchange('bingx', { syncing: true }), exchange()]),
    ).toEqual([]);
    expect(venuesWithFailedSync([])).toEqual([]);
  });
});

/**
 * The empty-state table (spec 022, "Empty state"), row by row. The first row that holds wins.
 */
describe('describeEmptyPositions', () => {
  const recomputeFailed: EmptyPositions = {
    kind: 'recompute_failed',
    at: RECOMPUTE_FAILED_AT,
    error: RECOMPUTE_ERROR,
  };

  /** A venue whose last sync failed, having stored nothing: rows 2 and 3 both hold. */
  const refusedKey = authFailedExchange('auth');
  /** A venue that has imported trades. */
  const imported = exchange({ exchange_key: 'bingx' });

  describe('row 1: the last recompute failed', () => {
    it.each<[string, PositionsResponse, ExchangeResponse[] | undefined]>([
      ['no snapshot, and no venue at all', failedFirstRecompute(), []],
      ['a snapshot behind it', emptySnapshot({ last_recompute: failedRecompute() }), [imported]],
      ['a venue whose sync failed too', failedFirstRecompute(), [refusedKey]],
      ['nothing imported, on a configured venue', failedFirstRecompute(), [unsyncedExchange()]],
      ['the exchanges query failed', failedFirstRecompute(), undefined],
    ])('wins over %s', (_label, data, exchanges) => {
      expect(describeEmptyPositions(data, exchanges)).toEqual(recomputeFailed);
    });
  });

  describe('row 2: a venue sync failed', () => {
    it('names the venue whose key was refused', () => {
      expect(describeEmptyPositions(emptySnapshot(), [refusedKey])).toEqual({
        kind: 'sync_failed',
        venues: ['bitget'],
      });
    });

    it('names every failed venue, and not the one that is fine', () => {
      expect(
        describeEmptyPositions(emptySnapshot(), [
          authFailedExchange('insufficient_scope', { exchange_key: 'bingx' }),
          erroredExchange('rate_limited'),
        ]),
      ).toEqual({ kind: 'sync_failed', venues: ['bingx', 'bitget'] });
      expect(
        describeEmptyPositions(emptySnapshot(), [imported, erroredExchange('unavailable')]),
      ).toEqual({ kind: 'sync_failed', venues: ['bitget'] });
    });

    it('wins over "no trades imported yet", which also holds', () => {
      // The refused venue stored nothing: "none imported" is true and would hide the reason.
      expect(refusedKey.fills_stored).toBe(0);
      expect(describeEmptyPositions(emptySnapshot(), [refusedKey]).kind).toBe('sync_failed');
    });

    it('wins over "not computed yet"', () => {
      expect(describeEmptyPositions(noSnapshot(), [refusedKey]).kind).toBe('sync_failed');
    });

    it('is skipped when the exchanges query failed', () => {
      // R8 S1: with no list, what the snapshot itself proves decides.
      expect(describeEmptyPositions(emptySnapshot(), undefined)).toEqual({
        kind: 'no_trades',
        anyConfigured: undefined,
      });
      expect(describeEmptyPositions(noSnapshot(), undefined)).toEqual({ kind: 'not_computed' });
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), undefined)).toEqual({
        kind: 'no_positions',
      });
    });
  });

  describe('row 3: no trades imported yet', () => {
    it('says no venue is configured when the list is empty', () => {
      expect(describeEmptyPositions(emptySnapshot(), [])).toEqual({
        kind: 'no_trades',
        anyConfigured: false,
      });
    });

    it('says no venue is configured when the only venue has lost its credentials', () => {
      const unconfigured = exchange({ configured: false, fills_stored: 0 });

      expect(describeEmptyPositions(emptySnapshot(), [unconfigured])).toEqual({
        kind: 'no_trades',
        anyConfigured: false,
      });
    });

    it('says a sync imports trades when a venue is configured and has stored none', () => {
      expect(
        describeEmptyPositions(emptySnapshot(), [
          unsyncedExchange('bingx'),
          unsyncedExchange('bitget', { syncing: true }),
        ]),
      ).toEqual({ kind: 'no_trades', anyConfigured: true });
    });

    it('wins over "not computed yet"', () => {
      expect(describeEmptyPositions(noSnapshot(), [])).toEqual({
        kind: 'no_trades',
        anyConfigured: false,
      });
    });

    it('holds with no exchange list when the snapshot replayed nothing, saying nothing of configuration', () => {
      // R8 S1: `event_count: 0` on a written snapshot proves no trade was replayed. Without
      // the list, whether a venue is configured is unknown, which is not "none is".
      const result = describeEmptyPositions(emptySnapshot(), undefined);

      expect(result).toEqual({ kind: 'no_trades', anyConfigured: undefined });
      expect(result).not.toEqual({ kind: 'no_trades', anyConfigured: false });
    });

    it('does not hold when the snapshot has replayed trades, whatever a lagging list says (R9 N-a)', () => {
      // A list polled before a stablecoin-only sync landed still says 0 fills everywhere.
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), [])).toEqual({
        kind: 'no_positions',
      });
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), [unsyncedExchange('bingx')])).toEqual(
        {
          kind: 'no_positions',
        },
      );
      // The same list with a snapshot over nothing, or none at all, still says "no trades".
      expect(describeEmptyPositions(emptySnapshot(), [unsyncedExchange('bingx')])).toEqual({
        kind: 'no_trades',
        anyConfigured: true,
      });
      expect(describeEmptyPositions(noSnapshot(), [unsyncedExchange('bingx')])).toEqual({
        kind: 'no_trades',
        anyConfigured: true,
      });
    });

    it('does not hold with no exchange list and no snapshot: that is not computed yet', () => {
      expect(describeEmptyPositions(noSnapshot(), undefined)).toEqual({ kind: 'not_computed' });
    });

    it('does not hold while any venue has stored a fill', () => {
      // A snapshot that replayed nothing while fills are stored predates them (row 4).
      expect(
        describeEmptyPositions(emptySnapshot(), [imported, unsyncedExchange('bitget')]).kind,
      ).toBe('not_computed');
    });
  });

  describe('row 4: not computed yet', () => {
    it('holds when fills are stored and no snapshot has been written', () => {
      expect(describeEmptyPositions(noSnapshot(), [imported])).toEqual({ kind: 'not_computed' });
    });

    it('holds when fills are stored and the snapshot replayed nothing: it predates them', () => {
      expect(describeEmptyPositions(emptySnapshot(), [imported])).toEqual({
        kind: 'not_computed',
      });
    });
  });

  describe('row 5: no positions', () => {
    it('holds for a snapshot over stablecoin trades only', () => {
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), [imported])).toEqual({
        kind: 'no_positions',
      });
    });

    it('holds only when the snapshot replayed something, whatever its last recompute did', () => {
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), undefined).kind).toBe('no_positions');
      expect(
        describeEmptyPositions(
          emptySnapshot({
            event_count: 4,
            last_recompute: { at: RECOMPUTE_FAILED_AT, outcome: 'unchanged', error: null },
          }),
          [imported],
        ).kind,
      ).toBe('no_positions');
      // The same snapshot over nothing is not "no positions".
      expect(describeEmptyPositions(emptySnapshot(), [imported]).kind).not.toBe('no_positions');
      expect(describeEmptyPositions(emptySnapshot(), undefined).kind).not.toBe('no_positions');
    });
  });
});
