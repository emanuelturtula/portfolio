import { describe, expect, it } from 'vitest';

import {
  describeClosedPositions,
  describeEmptyPositions,
  EXCLUSION_REASON_MESSAGES,
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  formatVenues,
  groupExclusions,
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
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
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
    expect(FLAG_EXPLANATIONS.unknown_basis).toMatch(/no known cost/);
    expect(FLAG_EXPLANATIONS.unknown_basis).toMatch(/left out of the portfolio totals/);
    expect(FLAG_EXPLANATIONS.history_incomplete).toMatch(/missing/);
    expect(FLAG_EXPLANATIONS.history_incomplete).toMatch(/realized P&L/);
    expect(FLAG_EXPLANATIONS.unattributed_fee).toMatch(/fee/);
  });

  it.each(ALL_EXCLUSION_REASONS)('gives the %s exclusion a reason', (reason) => {
    expect(EXCLUSION_REASON_MESSAGES[reason]).toMatch(/\.$/);
  });

  it('gives the two exclusions different reasons', () => {
    expect(EXCLUSION_REASON_MESSAGES.unknown_basis).toMatch(/no known cost/);
    expect(EXCLUSION_REASON_MESSAGES.unpriced).toMatch(/no price/);
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
  it('calls a fully sold position not held, as the wire spells it', () => {
    // The wire sends "0.000000000000000000", which is not the string "0".
    expect(isHeld(xrpClosed())).toBe(false);
    expect(isHeld(xrpClosed({ quantity: '0' }))).toBe(false);
  });

  it('calls the smallest amount the engine carries held', () => {
    expect(isHeld(position({ quantity: '0.000000000000000001' }))).toBe(true);
    expect(isHeld(position())).toBe(true);
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
  it('names one fully sold asset in the singular', () => {
    expect(describeClosedPositions(['XRP'])).toBe(
      '1 fully sold asset (XRP) is not listed; its realized P&L is in the total.',
    );
  });

  it('names several in the plural, in the order given', () => {
    expect(describeClosedPositions(['BTC', 'ETH'])).toBe(
      '2 fully sold assets (BTC, ETH) are not listed; their realized P&L is in the total.',
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
      expect(describeEmptyPositions(emptySnapshot(), undefined)).toEqual({ kind: 'no_positions' });
      expect(describeEmptyPositions(noSnapshot(), undefined)).toEqual({ kind: 'not_computed' });
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

    it('does not hold while any venue has stored a fill', () => {
      expect(
        describeEmptyPositions(emptySnapshot(), [imported, unsyncedExchange('bitget')]).kind,
      ).toBe('no_positions');
    });
  });

  describe('row 4: not computed yet', () => {
    it('holds when fills are stored and no snapshot has been written', () => {
      expect(describeEmptyPositions(noSnapshot(), [imported])).toEqual({ kind: 'not_computed' });
    });
  });

  describe('row 5: no positions', () => {
    it('holds for a snapshot over stablecoin trades only', () => {
      expect(describeEmptyPositions(stablecoinOnlySnapshot(), [imported])).toEqual({
        kind: 'no_positions',
      });
    });

    it('holds for a snapshot whose last recompute succeeded or changed nothing', () => {
      expect(describeEmptyPositions(emptySnapshot(), [imported]).kind).toBe('no_positions');
      expect(
        describeEmptyPositions(
          emptySnapshot({
            last_recompute: { at: RECOMPUTE_FAILED_AT, outcome: 'unchanged', error: null },
          }),
          [imported],
        ).kind,
      ).toBe('no_positions');
    });
  });
});
