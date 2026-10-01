import { describe, expect, it } from 'vitest';

import {
  ALL_QUANTITIES_MATCH,
  assetsWithUnreliableRealizedPnl,
  balanceReadings,
  balancesErrorSentence,
  describeBalancesFailure,
  describeClosedPositions,
  describeComparison,
  describeEmptyPositions,
  describeNeverRead,
  describeNotReadYet,
  describeOutOfDate,
  describeStaleWallets,
  describeSyncFailed,
  describeUnreadWallets,
  DIFFERENCE_LEGEND,
  EXCLUSION_REASON_MESSAGES,
  failedRecompute as failedRecomputeOf,
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  flagsOf,
  formatVenues,
  groupExclusions,
  hasNoKnownCost,
  HELD_EXCEEDS_HISTORY_BADGE,
  HELD_EXCEEDS_HISTORY_EXPLANATION,
  HELD_EXCEEDS_HISTORY_GUIDANCE,
  heldExceedsHistoryAssets,
  HISTORY_EXCEEDS_HELD_EXPLANATION,
  HOLDINGS_CHECK_ID,
  isHeld,
  MARKET_VALUE_UNAVAILABLE_MESSAGES,
  missingSources,
  NOTHING_TO_COMPARE,
  partitionReconciliation,
  venueLabel,
  venuesWithFailedSync,
  type EmptyPositions,
} from '@/lib/accounting';
import { errorSentence } from '@/lib/exchanges';
import { PRICE_UNAVAILABLE_MESSAGES } from '@/lib/prices';
import {
  ALL_EXCLUSION_REASONS,
  ALL_MARKET_VALUE_UNAVAILABLE,
  ALL_POSITION_FLAGS,
  ALL_PRICE_UNAVAILABLE,
  ALL_RECOMPUTE_OUTCOMES,
  bgbFeeNeverHeld,
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
  investedPortfolio,
  lastRecompute,
  noSnapshot,
  position,
  RECOMPUTE_ERROR,
  RECOMPUTE_FAILED_AT,
  stablecoinOnlySnapshot,
  xrpClosed,
  type PositionsResponse,
} from '@/test/accountingFixtures';
import {
  ALL_EXCHANGE_ERROR_KINDS,
  authFailedExchange,
  erroredExchange,
  exchange,
  unsyncedExchange,
  type ExchangeResponse,
} from '@/test/exchangeFixtures';
import {
  ALL_NOT_COMPARED_REASONS,
  ALL_RECONCILIATION_STATUSES,
  assetReconciliation,
  BALANCES_READ_AT,
  dogeWithinTolerance,
  EDGE_BALANCES_READ_AT,
  ethShortPrecise,
  exchangeBalances,
  failedBalances,
  kasNeverTraded,
  NO_WALLETS,
  notReconciled,
  OLD_BALANCES_READ_AT,
  OTHER_BALANCES_READ_AT,
  outOfDateBalances,
  reconciliation,
  solOver,
  syncFailedBalances,
  unreadBalances,
  walletReadings,
  WALLETS_OBSERVED_AT,
  xrpHeldElsewhere,
  type AssetReconciliationResponse,
  type ExchangeBalancesResponse,
  type NotComparedReason,
  type ReconciliationResponse,
  type ReconciliationStatus,
} from '@/test/reconciliationFixtures';

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
  /** No asset is held beyond its history: the holdings check found nothing, or has no reading. */
  const NONE: ReadonlySet<string> = new Set();

  it('names one asset no longer held in the singular', () => {
    expect(describeClosedPositions([xrpClosed()], NONE)).toBe(
      '1 asset no longer held is not listed: XRP. Its realized P&L is in the total.',
    );
  });

  it('names several in the plural, in the order given, with the flags beside the flagged ones', () => {
    // M1: history_incomplete is sticky, so the asset with the worst history is often the one
    // with no row. Its flag is named here or nowhere.
    expect(describeClosedPositions([bgbFeeNeverHeld(), xrpClosed()], NONE)).toBe(
      '2 assets no longer held are not listed: BGB (History incomplete), XRP. ' +
        'Their realized P&L is in the total.',
    );
  });

  it('names every flag a closed asset carries, by its badge text', () => {
    expect(
      describeClosedPositions(
        [xrpClosed({ flags: ['history_incomplete', 'unattributed_fee'] })],
        NONE,
      ),
    ).toBe(
      '1 asset no longer held is not listed: XRP (History incomplete, Fee not valued). ' +
        'Its realized P&L is in the total.',
    );
  });

  it('names "Held exceeds history" beside a closed asset the balances still hold (spec 025, R2)', () => {
    // The history says XRP is no longer held while an exchange holds it: the sharpest form of
    // the finding, on an asset with no row to carry a badge.
    expect(describeClosedPositions([xrpClosed()], new Set(['XRP']))).toBe(
      '1 asset no longer held is not listed: XRP (Held exceeds history). ' +
        'Its realized P&L is in the total.',
    );
  });

  it('puts it after the flags, and only beside the assets it is true of', () => {
    expect(
      describeClosedPositions(
        [bgbFeeNeverHeld(), xrpClosed({ asset: 'DOGE' }), xrpClosed()],
        new Set(['BGB', 'XRP']),
      ),
    ).toBe(
      '3 assets no longer held are not listed: ' +
        'BGB (History incomplete, Held exceeds history), DOGE, XRP (Held exceeds history). ' +
        'Their realized P&L is in the total.',
    );
  });

  it('says nothing of a held asset that is in the set: its row carries the badge', () => {
    // BTC is held beyond its history; the closed line is about XRP only.
    expect(describeClosedPositions([xrpClosed()], new Set(['BTC']))).toBe(
      '1 asset no longer held is not listed: XRP. Its realized P&L is in the total.',
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

/*
 * The holdings check (spec 025). Every sentence is written out here rather than imported: each
 * is a statement to the owner about what they hold, and a change to one should be a diff.
 */

/** Every asset the page tests use, one of each status and more: sorted by asset. */
function everyStatus(overrides: Partial<ReconciliationResponse> = {}) {
  return reconciliation({
    assets: [
      assetReconciliation(),
      dogeWithinTolerance(),
      ethShortPrecise(),
      kasNeverTraded(),
      solOver(),
      xrpHeldElsewhere(),
    ],
    wallets: walletReadings(2),
    ...overrides,
  });
}

/** One asset of each status, as the backend derives it. A new status fails `tsc` here. */
const ONE_OF_EACH: Record<ReconciliationStatus, AssetReconciliationResponse> = {
  match: dogeWithinTolerance(),
  history_short: ethShortPrecise(),
  history_over: solOver(),
};

/** One venue left out for each reason, as the backend writes it. A new reason fails `tsc`. */
const LEFT_OUT: Record<NotComparedReason, ExchangeBalancesResponse> = {
  read_failed: failedBalances('bitget', 'unavailable'),
  never_read: unreadBalances('bitget'),
  sync_failed: syncFailedBalances('bitget'),
  out_of_date: outOfDateBalances('bitget'),
};

describe('the holdings check words', () => {
  it('anchors the block where the badge points, and words the badge as the spec does', () => {
    expect(HOLDINGS_CHECK_ID).toBe('holdings-check');
    expect(HELD_EXCEEDS_HISTORY_BADGE).toBe('Held exceeds history');
  });

  it('says what is compared, and that a difference of the tolerance or less is a match', () => {
    // "or less": the rule is `<=`, and "under 1%" would put a difference of exactly 1% outside.
    expect(describeComparison('1')).toBe(
      'Compares what the history says is held with the balances read from your wallets and ' +
        "from your exchanges' spot accounts. A difference of 1% or less counts as a match.",
    );
    expect(describeComparison('1')).not.toMatch(/under/);
    // The percentage is the response's, not a copy of the rule kept in the page.
    expect(describeComparison('2.5')).toMatch(/A difference of 2\.5% or less counts as a match\.$/);
    expect(describeComparison('0.50')).toMatch(/of 0\.5% or less/);
  });

  it('refuses a tolerance that is not a plain decimal rather than printing it', () => {
    expect(() => describeComparison('1e0')).toThrow(TypeError);
  });

  it('tells the owner of a short history the usual cause, what to rule out first, and the way out (R9, R10)', () => {
    expect(HELD_EXCEEDS_HISTORY_GUIDANCE).toBe(
      'The balances read hold more than the history accounts for. The usual cause is buys ' +
        'older than an exchange keeps, or coins acquired elsewhere, and average cost and profit ' +
        'then leave those units out. Before recording anything, rule out coins in transit: ' +
        'readings are taken at different moments, so coins moved between two of them are ' +
        'counted twice until both have been read again. How old each reading is, is shown ' +
        'below the lists. If the gap is real, an opening balance records what is really ' +
        'missing: see "Recording what the history does not show" in docs/accounting.md.',
    );
    // R10: the double count lasts until both sources have been read again, which one sync does
    // not promise - a source that has stopped being read keeps its reading for up to a day.
    expect(HELD_EXCEEDS_HISTORY_GUIDANCE).not.toMatch(/until the next sync/);
    // A prompt to look, not a verdict: the cause is "usual", and nothing is said to be missing
    // before the owner has ruled out coins in transit.
    expect(HELD_EXCEEDS_HISTORY_GUIDANCE).not.toMatch(/are missing from the history/);
    expect(HELD_EXCEEDS_HISTORY_GUIDANCE.indexOf('rule out coins in transit')).toBeLessThan(
      HELD_EXCEEDS_HISTORY_GUIDANCE.indexOf('an opening balance records'),
    );
  });

  it('names the causes of a history above the balances, and says it cannot tell them apart (R9)', () => {
    expect(HISTORY_EXCEEDS_HELD_EXPLANATION).toBe(
      'The history accounts for more than the balances read. The causes include coins held ' +
        'where this application does not read them (another wallet, an Earn product, or a ' +
        'futures or funding account), withdrawals, network fees and trading fees the import ' +
        'did not record, and a sale or conversion the import did not see. This check cannot ' +
        'tell them apart, so it flags nothing.',
    );
    // A sale the import did not see is a real gap: the text no longer promises there is none.
    expect(HISTORY_EXCEEDS_HELD_EXPLANATION).not.toMatch(/Nothing needs correcting|is expected/);
  });

  it('explains the badge beside the flags, as a usual cause and not as a fact', () => {
    expect(HELD_EXCEEDS_HISTORY_EXPLANATION).toBe(
      'The balances read hold more of this asset than the history accounts for. The usual ' +
        'cause is a buy the history does not show, which leaves those units out of its average ' +
        'cost and profit; coins in transit between two readings can look the same. The ' +
        'holdings check below says more.',
    );
    // Not one of the position flags: it comes from the holdings check.
    expect(Object.values(FLAG_BADGES)).not.toContain(HELD_EXCEEDS_HISTORY_BADGE);
  });

  it('says which way the difference is taken, and has a line for each quiet outcome', () => {
    expect(DIFFERENCE_LEGEND).toBe(
      'Difference is the wallets plus the exchanges, minus what the history accounts for.',
    );
    expect(ALL_QUANTITIES_MATCH).toBe('Every quantity matches the balances read.');
    expect(NOTHING_TO_COMPARE).toBe('There is nothing to compare yet.');
  });
});

describe('partitionReconciliation', () => {
  it('puts each asset in the list its status names, in the endpoint order, and a match in neither', () => {
    const { short, over } = partitionReconciliation(everyStatus().assets);

    expect(short.map((entry) => entry.asset)).toEqual(['BTC', 'ETH', 'KAS']);
    expect(over.map((entry) => entry.asset)).toEqual(['SOL', 'XRP']);
    // DOGE is within the tolerance, with a difference that is not zero: listed nowhere.
    expect([...short, ...over].map((entry) => entry.asset)).not.toContain('DOGE');
  });

  it('hands the entries over untouched', () => {
    const response = everyStatus();
    const { short, over } = partitionReconciliation(response.assets);

    expect(short[1]).toBe(response.assets[2]);
    expect(over[0]).toBe(response.assets[4]);
  });

  it.each(ALL_RECONCILIATION_STATUSES)('places a lone %s asset', (status) => {
    const entry = ONE_OF_EACH[status];
    const { short, over } = partitionReconciliation([entry]);

    expect(short).toEqual(status === 'history_short' ? [entry] : []);
    expect(over).toEqual(status === 'history_over' ? [entry] : []);
  });

  it('partitions nothing into nothing', () => {
    expect(partitionReconciliation([])).toEqual({ short: [], over: [] });
  });
});

describe('failedRecompute', () => {
  it('is the record of a recompute that failed, and null for any other', () => {
    expect(failedRecomputeOf(failedRecompute())).toEqual({
      at: RECOMPUTE_FAILED_AT,
      outcome: 'failed',
      error: RECOMPUTE_ERROR,
    });
    expect(failedRecomputeOf(null)).toBeNull();
  });

  it.each(ALL_RECOMPUTE_OUTCOMES)(
    'treats a recompute that was %s as failed only when it is',
    (outcome) => {
      const record = lastRecompute({
        outcome,
        error: outcome === 'failed' ? RECOMPUTE_ERROR : null,
      });

      expect(failedRecomputeOf(record)).toBe(outcome === 'failed' ? record : null);
    },
  );
});

describe('heldExceedsHistoryAssets', () => {
  it('names the history_short assets, and no other', () => {
    expect([...heldExceedsHistoryAssets(everyStatus())]).toEqual(['BTC', 'ETH', 'KAS']);
  });

  it.each(ALL_RECONCILIATION_STATUSES)('names a %s asset only when it is short', (status) => {
    const entry = ONE_OF_EACH[status];
    const names = heldExceedsHistoryAssets(reconciliation({ assets: [entry] }));

    expect(names.has(entry.asset)).toBe(status === 'history_short');
    expect(names.size).toBe(status === 'history_short' ? 1 : 0);
  });

  it('is empty while the reconciliation is loading or failed: no reading, no badge', () => {
    expect(heldExceedsHistoryAssets(undefined).size).toBe(0);
  });

  it('is empty with no snapshot, and when everything matches', () => {
    expect(heldExceedsHistoryAssets(notReconciled()).size).toBe(0);
    expect(heldExceedsHistoryAssets(reconciliation({ assets: [dogeWithinTolerance()] })).size).toBe(
      0,
    );
  });

  it('is empty while the last recompute has failed: the block shows no comparison either (R9)', () => {
    // The same assets, still served. A history older than the balances shows every asset
    // bought since as short, so nothing is drawn from it.
    const stale = everyStatus({ last_recompute: failedRecompute() });

    expect(stale.assets.filter((entry) => entry.status === 'history_short')).toHaveLength(3);
    expect(heldExceedsHistoryAssets(stale).size).toBe(0);
  });

  it.each(ALL_RECOMPUTE_OUTCOMES.filter((outcome) => outcome !== 'failed'))(
    'still names them after a recompute that was %s, and after none at all',
    (outcome) => {
      const current = everyStatus({ last_recompute: lastRecompute({ outcome }) });

      expect(heldExceedsHistoryAssets(current).size).toBe(3);
      expect(heldExceedsHistoryAssets(everyStatus({ last_recompute: null })).size).toBe(3);
    },
  );
});

describe('balancesErrorSentence', () => {
  const OTHER_KINDS = ALL_EXCHANGE_ERROR_KINDS.filter(
    (kind) => kind !== 'internal' && kind !== 'auth' && kind !== 'insufficient_scope',
  );

  it.each(OTHER_KINDS)('reuses the exchanges page sentence for %s, naming the venue', (kind) => {
    // One sentence per kind across the two pages, so they cannot drift.
    expect(balancesErrorSentence(kind, 'Bitget')).toBe(errorSentence(kind, 'Bitget'));
    expect(balancesErrorSentence(kind, 'Bitget')).toMatch(/Bitget/);
  });

  it.each(['auth', 'insufficient_scope'] as const)(
    'says a %s refusal was of the balance read, that it is not retried, and how to retry (R9)',
    (kind) => {
      expect(balancesErrorSentence(kind, 'Bitget')).toBe(
        'Bitget refused the API key for the balance read. Scheduled syncs will not ask again; ' +
          'a sync from the Exchanges page retries once the key is fixed.',
      );
      // Not the exchanges page's sentence: "does not have read permission" is untrue of a key
      // that has just read the trades.
      expect(balancesErrorSentence(kind, 'Bitget')).not.toBe(errorSentence(kind, 'Bitget'));
      expect(balancesErrorSentence(kind, 'BingX')).toMatch(/^BingX refused the API key/);
    },
  );

  it('does not say the sync stopped for a defect of ours: the fills are in, the read failed', () => {
    expect(balancesErrorSentence('internal', 'Bitget')).toBe(
      'A defect in this application stopped the balances from being read. ' +
        'The container log has the details.',
    );
    expect(balancesErrorSentence('internal', 'Bitget')).not.toBe(
      errorSentence('internal', 'Bitget'),
    );
    expect(balancesErrorSentence('internal', 'Bitget')).not.toMatch(/Bitget|stopped the sync/);
  });

  it.each(ALL_EXCHANGE_ERROR_KINDS)('has a whole sentence for %s', (kind) => {
    expect(balancesErrorSentence(kind, 'BingX')).toMatch(/^[A-Z].*\.$/);
  });
});

describe('missingSources', () => {
  it('names nothing when every venue is compared and no wallet is stale or unread', () => {
    expect(missingSources(reconciliation())).toEqual([]);
    expect(missingSources(reconciliation({ exchanges: [], wallets: NO_WALLETS }))).toEqual([]);
    expect(
      missingSources(
        reconciliation({
          exchanges: [exchangeBalances({ exchange_key: 'bingx' }), exchangeBalances()],
          wallets: walletReadings(3),
        }),
      ),
    ).toEqual([]);
  });

  it('read_failed: the error, and the last good reading that is not used', () => {
    expect(
      missingSources(reconciliation({ exchanges: [failedBalances('bitget', 'unavailable')] })),
    ).toEqual([
      {
        kind: 'read_failed',
        venue: 'bitget',
        error: 'unavailable',
        lastReadAt: OLD_BALANCES_READ_AT,
      },
    ]);
  });

  it('read_failed with no reading at all: read_failed still, the first reason that applies', () => {
    expect(
      missingSources(reconciliation({ exchanges: [failedBalances('bingx', 'auth', null)] })),
    ).toEqual([{ kind: 'read_failed', venue: 'bingx', error: 'auth', lastReadAt: null }]);
  });

  it('never_read: named as not read yet rather than as failed', () => {
    expect(missingSources(reconciliation({ exchanges: [unreadBalances('bitget')] }))).toEqual([
      { kind: 'never_read', venue: 'bitget' },
    ]);
  });

  it('sync_failed: the reading it keeps, however recent, with no age limit in it', () => {
    expect(missingSources(reconciliation({ exchanges: [syncFailedBalances('bitget')] }))).toEqual([
      { kind: 'sync_failed', venue: 'bitget', lastReadAt: OLD_BALANCES_READ_AT },
    ]);
    // A sync that failed after a reading 14 minutes old: left out all the same.
    expect(
      missingSources(
        reconciliation({ exchanges: [syncFailedBalances('bingx', BALANCES_READ_AT)] }),
      ),
    ).toEqual([{ kind: 'sync_failed', venue: 'bingx', lastReadAt: BALANCES_READ_AT }]);
  });

  it('out_of_date: the reading and the age limit the response states', () => {
    expect(missingSources(reconciliation({ exchanges: [outOfDateBalances('bitget')] }))).toEqual([
      { kind: 'out_of_date', venue: 'bitget', lastReadAt: OLD_BALANCES_READ_AT, maxAgeHours: 24 },
    ]);
    // The limit is the response's: at 48 hours a reading three days old is still out of date.
    expect(
      missingSources(
        reconciliation({ max_reading_age_hours: 48, exchanges: [outOfDateBalances('bitget')] }),
      ),
    ).toEqual([
      { kind: 'out_of_date', venue: 'bitget', lastReadAt: OLD_BALANCES_READ_AT, maxAgeHours: 48 },
    ]);
  });

  it.each(ALL_NOT_COMPARED_REASONS)(
    'gives a venue left out as %s exactly one notice, of that kind',
    (reason) => {
      const sources = missingSources(reconciliation({ exchanges: [LEFT_OUT[reason]] }));

      expect(sources.map((source) => source.kind)).toEqual([reason]);
    },
  );

  it('goes by the reason, not by the age of the reading: a compared venue at the limit has no notice', () => {
    expect(
      missingSources(
        reconciliation({
          exchanges: [exchangeBalances({ balances_read_at: EDGE_BALANCES_READ_AT })],
        }),
      ),
    ).toEqual([]);
  });

  it('counts the stale wallets, with the age limit, and says nothing of them at zero', () => {
    expect(missingSources(reconciliation({ wallets: walletReadings(2, { stale: 1 }) }))).toEqual([
      { kind: 'wallets_stale', count: 1, maxAgeHours: 24 },
    ]);
    expect(missingSources(reconciliation({ wallets: walletReadings(0, { stale: 3 }) }))).toEqual([
      { kind: 'wallets_stale', count: 3, maxAgeHours: 24 },
    ]);
    expect(missingSources(reconciliation({ wallets: walletReadings(2, { stale: 0 }) }))).toEqual(
      [],
    );
  });

  it('counts the wallets no sync has read, and says nothing of them at zero', () => {
    expect(missingSources(reconciliation({ wallets: walletReadings(2, { unread: 1 }) }))).toEqual([
      { kind: 'wallets_unread', count: 1 },
    ]);
    expect(missingSources(reconciliation({ wallets: walletReadings(0, { unread: 3 }) }))).toEqual([
      { kind: 'wallets_unread', count: 3 },
    ]);
    expect(missingSources(reconciliation({ wallets: walletReadings(2, { unread: 0 }) }))).toEqual(
      [],
    );
  });

  it('keeps stale and unread wallets apart, each with its own count', () => {
    expect(
      missingSources(reconciliation({ wallets: walletReadings(1, { stale: 2, unread: 5 }) })),
    ).toEqual([
      { kind: 'wallets_stale', count: 2, maxAgeHours: 24 },
      { kind: 'wallets_unread', count: 5 },
    ]);
  });

  it('lists the venues first, in the endpoint order, then the stale wallets, then the unread ones', () => {
    expect(
      missingSources(
        reconciliation({
          exchanges: [unreadBalances('bingx'), failedBalances('bitget', 'schema')],
          wallets: walletReadings(1, { stale: 1, unread: 2 }),
        }),
      ),
    ).toEqual([
      { kind: 'never_read', venue: 'bingx' },
      {
        kind: 'read_failed',
        venue: 'bitget',
        error: 'schema',
        lastReadAt: OLD_BALANCES_READ_AT,
      },
      { kind: 'wallets_stale', count: 1, maxAgeHours: 24 },
      { kind: 'wallets_unread', count: 2 },
    ]);
  });

  it('names a venue left out beside one that is compared, and only the one left out', () => {
    expect(
      missingSources(
        reconciliation({
          exchanges: [outOfDateBalances('bingx'), exchangeBalances()],
        }),
      ).map((source) => ('venue' in source ? source.venue : source.kind)),
    ).toEqual(['bingx']);
  });

  it('still names what is left out when there is no snapshot to compare', () => {
    // The page renders nothing for it; the rule itself does not depend on `computed_at`.
    expect(missingSources(notReconciled({ exchanges: [unreadBalances('bitget')] }))).toHaveLength(
      1,
    );
  });
});

describe('the source notices', () => {
  it('says a venue could not be read and why, by its display name', () => {
    expect(describeBalancesFailure('bitget', 'unavailable')).toBe(
      'The balances at Bitget could not be read. ' +
        'Bitget could not be reached, or answered that it was unavailable.',
    );
    expect(describeBalancesFailure('bingx', 'auth')).toBe(
      'The balances at BingX could not be read. BingX refused the API key for the balance ' +
        'read. Scheduled syncs will not ask again; a sync from the Exchanges page retries once ' +
        'the key is fixed.',
    );
    expect(describeBalancesFailure('bingx', 'internal')).toBe(
      'The balances at BingX could not be read. A defect in this application stopped the ' +
        'balances from being read. The container log has the details.',
    );
  });

  it.each(ALL_EXCHANGE_ERROR_KINDS)(
    'gives a %s failure its own reason after the same opening',
    (kind) => {
      expect(describeBalancesFailure('bitget', kind)).toBe(
        `The balances at Bitget could not be read. ${balancesErrorSentence(kind, 'Bitget')}`,
      );
    },
  );

  it('says the coins of a venue whose read failed before any reading are left out', () => {
    expect(describeNeverRead('bingx')).toBe(
      'No balances have been read from BingX, so the coins held there are left out of the ' +
        'comparison.',
    );
  });

  it('says when a venue with no error and no reading will be read', () => {
    expect(describeNotReadYet('bitget')).toBe(
      'The balances at Bitget have not been read yet. They are read after its next successful ' +
        'sync, and until then the coins held there are left out of the comparison.',
    );
    expect(describeNotReadYet('bingx')).toMatch(/^The balances at BingX have not been read yet\./);
  });

  it('ends every notice of a source left out with the same words (R10)', () => {
    const LEFT_OUT_OF = /left out of the comparison\.$/;

    expect(describeNeverRead('bitget')).toMatch(LEFT_OUT_OF);
    expect(describeNotReadYet('bitget')).toMatch(LEFT_OUT_OF);
    expect(describeSyncFailed('bitget')).toMatch(LEFT_OUT_OF);
    expect(describeOutOfDate(24)).toMatch(LEFT_OUT_OF);
    expect(describeStaleWallets(1, 24)).toMatch(LEFT_OUT_OF);
    expect(describeStaleWallets(2, 24)).toMatch(LEFT_OUT_OF);
    expect(describeUnreadWallets(1)).toMatch(LEFT_OUT_OF);
    expect(describeUnreadWallets(2)).toMatch(LEFT_OUT_OF);
  });

  it('says a failed sync left the balances unread, and that they are left out', () => {
    expect(describeSyncFailed('bitget')).toBe(
      'The last sync of Bitget failed, so its balances were not read and are left out of the ' +
        'comparison.',
    );
    expect(describeSyncFailed('bingx')).toMatch(/^The last sync of BingX failed/);
  });

  it('states the age limit as the rule that leaves an old reading out', () => {
    expect(describeOutOfDate(24)).toBe(
      'A reading older than 24 hours is left out of the comparison.',
    );
    expect(describeOutOfDate(48)).toBe(
      'A reading older than 48 hours is left out of the comparison.',
    );
  });

  it('counts the stale wallets in the singular and in the plural, with the age limit', () => {
    expect(describeStaleWallets(1, 24)).toBe(
      '1 wallet was last read more than 24 hours ago, so the coins in it are left out of the ' +
        'comparison.',
    );
    expect(describeStaleWallets(2, 24)).toBe(
      '2 wallets were last read more than 24 hours ago, so the coins in them are left out of ' +
        'the comparison.',
    );
    expect(describeStaleWallets(11, 48)).toBe(
      '11 wallets were last read more than 48 hours ago, so the coins in them are left out of ' +
        'the comparison.',
    );
  });

  it('counts the unread wallets in the singular and in the plural', () => {
    expect(describeUnreadWallets(1)).toBe(
      '1 wallet has not been read yet, so the coins in it are left out of the comparison.',
    );
    expect(describeUnreadWallets(2)).toBe(
      '2 wallets have not been read yet, so the coins in them are left out of the comparison.',
    );
    expect(describeUnreadWallets(11)).toBe(
      '11 wallets have not been read yet, so the coins in them are left out of the comparison.',
    );
  });
});

describe('balanceReadings', () => {
  it('lists each venue that was compared by its display name, then the oldest wallet reading', () => {
    expect(
      balanceReadings(
        reconciliation({
          exchanges: [
            exchangeBalances({ exchange_key: 'bingx', balances_read_at: OTHER_BALANCES_READ_AT }),
            exchangeBalances(),
          ],
          wallets: walletReadings(2),
        }),
      ),
    ).toEqual([
      { label: 'BingX', at: OTHER_BALANCES_READ_AT },
      { label: 'Bitget', at: BALANCES_READ_AT },
      { label: 'Wallets (oldest reading)', at: WALLETS_OBSERVED_AT },
    ]);
  });

  it.each(ALL_NOT_COMPARED_REASONS)(
    'leaves out a venue left out as %s, reading or not: a time beside a figure not in the sum would read as if it were (R9)',
    (reason) => {
      expect(balanceReadings(reconciliation({ exchanges: [LEFT_OUT[reason]] }))).toEqual([]);
      // Beside a compared venue, only the compared one is listed.
      expect(
        balanceReadings(
          reconciliation({
            exchanges: [{ ...LEFT_OUT[reason], exchange_key: 'bingx' }, exchangeBalances()],
          }),
        ),
      ).toEqual([{ label: 'Bitget', at: BALANCES_READ_AT }]);
    },
  );

  it('leaves out a failed venue that has never been read, too', () => {
    expect(
      balanceReadings(reconciliation({ exchanges: [failedBalances('bingx', 'auth', null)] })),
    ).toEqual([]);
  });

  it('lists a compared venue at the very edge of the age limit', () => {
    expect(
      balanceReadings(
        reconciliation({
          exchanges: [exchangeBalances({ balances_read_at: EDGE_BALANCES_READ_AT })],
        }),
      ),
    ).toEqual([{ label: 'Bitget', at: EDGE_BALANCES_READ_AT }]);
  });

  it('leaves out the wallets when none is compared', () => {
    expect(
      balanceReadings(reconciliation({ wallets: walletReadings(0, { stale: 1, unread: 2 }) })),
    ).toEqual([{ label: 'Bitget', at: BALANCES_READ_AT }]);
    expect(balanceReadings(reconciliation({ exchanges: [], wallets: NO_WALLETS }))).toEqual([]);
  });
});
