import { describe, expect, it } from 'vitest';

import {
  breakEvenPortfolio,
  COMPUTED_AT,
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
  investedPortfolio,
  lastRecompute,
  noSnapshot,
  position,
  positionsResponse,
  totals,
  ZERO,
} from './accountingFixtures';
import { fakeAccounting } from './fakeAccounting';
import {
  ALL_NOT_COMPARED_REASONS,
  assertSameSnapshot,
  assetReconciliation,
  BALANCES_READ_AT,
  BTC_BELOW,
  BTC_BEYOND,
  btcBelowPosition,
  dogeWithinTolerance,
  EDGE_BALANCES_READ_AT,
  ETH_BEYOND,
  ethShortPrecise,
  exchangeBalances,
  failedBalances,
  failedChain,
  investedPortfolioGaps,
  kasNeverTraded,
  matchedAsset,
  matchingReconciliation,
  MAX_READING_AGE_HOURS,
  NO_WALLETS,
  notReconciled,
  OLD_BALANCES_READ_AT,
  outOfDateBalances,
  PAST_EDGE_BALANCES_READ_AT,
  reconciliation,
  solOver,
  syncFailedBalances,
  unreadBalances,
  walletReadings,
  WALLETS_OBSERVED_AT,
  XRP_LEFT,
  xrpHeldElsewhere,
} from './reconciliationFixtures';

/**
 * The control on the reconciliation fixture guard. Its silence on every scenario the page
 * tests use means something only if it speaks on the states `reconcile` cannot produce.
 */
describe('the reconciliation fixture guard', () => {
  it('accepts every asset the page tests use, in one response', () => {
    expect(() =>
      reconciliation({
        assets: [
          assetReconciliation(),
          dogeWithinTolerance(),
          ethShortPrecise(),
          kasNeverTraded(),
          solOver(),
          xrpHeldElsewhere(),
        ],
        wallets: walletReadings(2),
      }),
    ).not.toThrow();
  });

  it('accepts every state a source can be in', () => {
    expect(() =>
      reconciliation({
        exchanges: [failedBalances('bingx', 'unavailable'), unreadBalances('bitget')],
        wallets: walletReadings(1, { stale: 1, unread: 2 }),
      }),
    ).not.toThrow();
    expect(() =>
      reconciliation({ exchanges: [failedBalances('bitget', 'auth', null)] }),
    ).not.toThrow();
    expect(() =>
      reconciliation({ exchanges: [syncFailedBalances('bingx'), outOfDateBalances('bitget')] }),
    ).not.toThrow();
    // A sync that failed minutes ago leaves a young reading, and it is still not compared.
    expect(() =>
      reconciliation({ exchanges: [syncFailedBalances('bitget', BALANCES_READ_AT)] }),
    ).not.toThrow();
    expect(() => reconciliation({ exchanges: [] })).not.toThrow();
  });

  it.each(ALL_NOT_COMPARED_REASONS)('has a builder for a venue left out as %s', (reason) => {
    const built = {
      read_failed: failedBalances('bitget', 'schema'),
      never_read: unreadBalances('bitget'),
      sync_failed: syncFailedBalances('bitget'),
      out_of_date: outOfDateBalances('bitget'),
    }[reason];

    expect(built.not_compared_reason).toBe(reason);
    expect(() => reconciliation({ exchanges: [built] })).not.toThrow();
  });

  it('holds each reason to the fields the reasons before it rule out', () => {
    // An error is read_failed, whatever else is true of the venue.
    for (const reason of [null, 'never_read', 'sync_failed', 'out_of_date'] as const) {
      expect(() =>
        reconciliation({
          exchanges: [{ ...failedBalances('bitget', 'auth'), not_compared_reason: reason }],
        }),
      ).toThrow('a failed balance read is read_failed');
    }
    // No error and no reading is never_read.
    for (const reason of [null, 'read_failed', 'sync_failed', 'out_of_date'] as const) {
      expect(() =>
        reconciliation({
          exchanges: [{ ...unreadBalances('bitget'), not_compared_reason: reason }],
        }),
      ).toThrow('bitget: no error and no reading is never_read');
    }
    // A venue with a reading and no error is neither of the first two.
    for (const reason of ['read_failed', 'never_read'] as const) {
      expect(() =>
        reconciliation({ exchanges: [exchangeBalances({ not_compared_reason: reason })] }),
      ).toThrow(`${reason} is for a venue with an error, or with no reading at all`);
    }
  });

  it('measures a reading against the age limit: at most that old is current', () => {
    // Exactly 24 hours old is compared; one second older is not.
    expect(() =>
      reconciliation({
        exchanges: [exchangeBalances({ balances_read_at: EDGE_BALANCES_READ_AT })],
      }),
    ).not.toThrow();
    expect(() =>
      reconciliation({ exchanges: [outOfDateBalances('bitget', PAST_EDGE_BALANCES_READ_AT)] }),
    ).not.toThrow();
    expect(() =>
      reconciliation({
        exchanges: [exchangeBalances({ balances_read_at: PAST_EDGE_BALANCES_READ_AT })],
      }),
    ).toThrow('a reading older than the age limit is not compared');
    expect(() =>
      reconciliation({ exchanges: [outOfDateBalances('bitget', EDGE_BALANCES_READ_AT)] }),
    ).toThrow('a reading at most the age limit old is current, not out_of_date');
    // The limit is the response's own.
    expect(() =>
      reconciliation({
        max_reading_age_hours: 96,
        exchanges: [exchangeBalances({ balances_read_at: OLD_BALANCES_READ_AT })],
      }),
    ).not.toThrow();
    expect(() => reconciliation({ max_reading_age_hours: 0 })).toThrow('whole number of hours');
  });

  it('refuses a compared wallet reading older than the age limit', () => {
    expect(() =>
      reconciliation({
        wallets: { ...NO_WALLETS, compared: 1, oldest_observed_at: OLD_BALANCES_READ_AT },
      }),
    ).toThrow('of a compared wallet, so at most the age limit old');
  });

  it('holds last_recompute to what the trigger records', () => {
    expect(() => reconciliation({ last_recompute: failedRecompute() })).not.toThrow();
    expect(() => reconciliation({ last_recompute: null })).not.toThrow();
    expect(() =>
      reconciliation({ last_recompute: lastRecompute({ outcome: 'failed', error: null }) }),
    ).toThrow('a failed recompute records its error class');
    // With no snapshot, the only recompute there can have been is one that failed.
    expect(() => notReconciled({ last_recompute: failedRecompute() })).not.toThrow();
    expect(() => notReconciled({ last_recompute: lastRecompute() })).toThrow(
      'left a snapshot behind it',
    );
  });

  it('accepts no snapshot, with the sources still answered', () => {
    const response = notReconciled({
      exchanges: [exchangeBalances()],
      wallets: walletReadings(1, { unread: 1 }),
    });

    expect(response.computed_at).toBeNull();
    expect(response.assets).toEqual([]);
    expect(response.exchanges).toHaveLength(1);
  });

  it('refuses assets with no snapshot: "not computed" is never "all unaccounted for"', () => {
    expect(() =>
      reconciliation({
        computed_at: null,
        assets: [kasNeverTraded()],
        wallets: walletReadings(1),
      }),
    ).toThrow('with no snapshot there is nothing to compare');
  });

  it('refuses a held quantity that is not the wallets plus the exchanges', () => {
    expect(() =>
      reconciliation({
        assets: [assetReconciliation({ held_quantity: '1.100000000000000000' })],
        wallets: walletReadings(1),
      }),
    ).toThrow('BTC held_quantity is 1.100000000000000000; the wallets and the exchanges add up to');
  });

  it('refuses a difference that is not held minus history', () => {
    expect(() =>
      reconciliation({
        assets: [assetReconciliation({ difference: '-0.500000000000000000' })],
        wallets: walletReadings(1),
      }),
    ).toThrow('BTC difference is -0.500000000000000000; held minus history is 0.5');
  });

  it('refuses a status the figures do not give, each way', () => {
    // +0.5 on a history of 0.5 is not a match, and it is not "over".
    expect(() =>
      reconciliation({
        assets: [assetReconciliation({ status: 'match' })],
        wallets: walletReadings(1),
      }),
    ).toThrow('BTC is stated match');
    expect(() =>
      reconciliation({
        assets: [assetReconciliation({ status: 'history_over' })],
        wallets: walletReadings(1),
      }),
    ).toThrow('is history_short');
    expect(() => reconciliation({ assets: [solOver({ status: 'history_short' })] })).toThrow(
      'is history_over',
    );
    // -5 on 1000 is half a percent: inside the tolerance.
    expect(() =>
      reconciliation({ assets: [{ ...dogeWithinTolerance(), status: 'history_over' }] }),
    ).toThrow('DOGE is stated history_over');
  });

  it('applies the tolerance exactly at its edge, with no rounding', () => {
    // History 99, held 100: 1 x 100 = 100 against 1 x max(99, 100) = 100. Equal, so a match.
    const atTheEdge = assetReconciliation({
      asset: 'ADA',
      history_quantity: '99.000000000000000000',
      wallet_quantity: ZERO,
      exchange_quantity: '100.000000000000000000',
      held_quantity: '100.000000000000000000',
      difference: '1.000000000000000000',
    });
    expect(() => reconciliation({ assets: [{ ...atTheEdge, status: 'match' }] })).not.toThrow();
    expect(() => reconciliation({ assets: [atTheEdge] })).toThrow('ADA is stated history_short');

    // One unit of the last place more: 100.000000000000000100 > 100.000000000000000001.
    const pastTheEdge = {
      ...atTheEdge,
      exchange_quantity: '100.000000000000000001',
      held_quantity: '100.000000000000000001',
      difference: '1.000000000000000001',
    };
    expect(() => reconciliation({ assets: [pastTheEdge] })).not.toThrow();
    expect(() => reconciliation({ assets: [{ ...pastTheEdge, status: 'match' }] })).toThrow(
      'ADA is stated match',
    );
  });

  it('derives the status from the tolerance the response states', () => {
    // -2.5 on 10 is 25 percent: over at 1 percent, a match at 25.
    expect(() =>
      reconciliation({ tolerance_pct: '25', assets: [solOver({ status: 'match' })] }),
    ).not.toThrow();
    expect(() => reconciliation({ tolerance_pct: '25', assets: [solOver()] })).toThrow(
      'a tolerance of 25 percent is match',
    );
    expect(() => reconciliation({ tolerance_pct: '1e0' })).toThrow('tolerance_pct');
  });

  it('refuses a quantity at the wrong scale, and one that is not a plain decimal', () => {
    expect(() => reconciliation({ assets: [matchedAsset('ADA', '5')] })).toThrow(
      'has 0 places; the backend sends 18',
    );
    expect(() => reconciliation({ assets: [matchedAsset('ADA', '5e3')] })).toThrow(
      'is not a plain decimal string',
    );
  });

  it('refuses a negative quantity on any side', () => {
    expect(() =>
      reconciliation({
        assets: [
          assetReconciliation({
            asset: 'ADA',
            history_quantity: ZERO,
            wallet_quantity: ZERO,
            exchange_quantity: '-1.000000000000000000',
            held_quantity: '-1.000000000000000000',
            difference: '-1.000000000000000000',
            status: 'history_over',
          }),
        ],
      }),
    ).toThrow('is never negative');
  });

  it('refuses a cash asset, and an asset with nothing on either side', () => {
    expect(() =>
      reconciliation({ assets: [matchedAsset('USDT', '100.000000000000000000')] }),
    ).toThrow('USDT is a cash asset');
    expect(() => reconciliation({ assets: [matchedAsset('ADA', ZERO)] })).toThrow(
      'nothing in the history and nothing held is left out',
    );
  });

  it('refuses assets out of order, and an asset named twice', () => {
    expect(() => reconciliation({ assets: [solOver(), ethShortPrecise()] })).toThrow(
      'sorted by asset',
    );
    expect(() => reconciliation({ assets: [solOver(), solOver()] })).toThrow('one per asset');
  });

  it('refuses a balance with no current reading behind it (R9)', () => {
    // A wallet quantity, and no wallet compared: none registered, or every one stale or unread.
    expect(() => reconciliation({ assets: [assetReconciliation()] })).toThrow(
      'a wallet quantity comes from a wallet that is compared',
    );
    expect(() =>
      reconciliation({
        assets: [assetReconciliation()],
        wallets: walletReadings(0, { stale: 2, unread: 1 }),
      }),
    ).toThrow('a wallet quantity comes from a wallet that is compared');
    // An exchange quantity, and no venue compared. A reading that is not current contributes
    // nothing - that is the ruling - so each reason refuses it, and so does no account at all.
    for (const leftOut of [
      failedBalances('bitget', 'schema'),
      unreadBalances('bitget'),
      syncFailedBalances('bitget'),
      syncFailedBalances('bitget', BALANCES_READ_AT),
      outOfDateBalances('bitget'),
    ]) {
      expect(() => reconciliation({ assets: [solOver()], exchanges: [leftOut] })).toThrow(
        'an exchange quantity comes from a venue that is compared',
      );
    }
    expect(() => reconciliation({ assets: [solOver()], exchanges: [] })).toThrow(
      'an exchange quantity comes from a venue that is compared',
    );
    // One compared venue beside one left out is enough.
    expect(() =>
      reconciliation({
        assets: [solOver()],
        exchanges: [failedBalances('bingx', 'schema'), exchangeBalances()],
      }),
    ).not.toThrow();
  });

  it('refuses a wallet quantity of an asset no chain holds', () => {
    expect(() =>
      reconciliation({
        assets: [
          assetReconciliation({
            asset: 'SOL',
            history_quantity: ZERO,
            wallet_quantity: '1.000000000000000000',
            exchange_quantity: ZERO,
            held_quantity: '1.000000000000000000',
            difference: '1.000000000000000000',
          }),
        ],
        wallets: walletReadings(1),
      }),
    ).toThrow("SOL: only a chain's own asset");
  });

  it('refuses venues out of order or listed twice, and impossible wallet counts', () => {
    expect(() =>
      reconciliation({
        exchanges: [exchangeBalances(), exchangeBalances({ exchange_key: 'bingx' })],
      }),
    ).toThrow('by exchange_key');
    expect(() => reconciliation({ exchanges: [exchangeBalances(), exchangeBalances()] })).toThrow(
      'one entry per account',
    );
    expect(() =>
      reconciliation({ wallets: { ...NO_WALLETS, compared: 1, oldest_observed_at: null } }),
    ).toThrow('oldest_observed_at');
    expect(() =>
      reconciliation({ wallets: { ...NO_WALLETS, oldest_observed_at: BALANCES_READ_AT } }),
    ).toThrow('oldest_observed_at');
    // Stale and unread wallets have no reading that is compared, so they give no oldest one.
    expect(() =>
      reconciliation({
        wallets: { ...NO_WALLETS, stale: 2, unread: 1, oldest_observed_at: BALANCES_READ_AT },
      }),
    ).toThrow('oldest_observed_at');
    expect(() => reconciliation({ wallets: { ...NO_WALLETS, unread: -1 } })).toThrow(
      'non-negative integers',
    );
    expect(() => reconciliation({ wallets: { ...NO_WALLETS, stale: 1.5 } })).toThrow(
      'non-negative integers',
    );
  });

  it('accepts wallets left out because their chain failed, beside every other state (spec 028)', () => {
    // The spec's own document: one wallet compared, two left out on Bitcoin.
    expect(
      reconciliation({ wallets: walletReadings(1, { failedChains: [failedChain('bitcoin', 2)] }) })
        .wallets,
    ).toEqual({
      compared: 1,
      stale: 0,
      unread: 0,
      chain_failed: 2,
      failed_chains: [{ chain_key: 'bitcoin', wallets: 2 }],
      oldest_observed_at: WALLETS_OBSERVED_AT,
    });
    // Both chains failed, beside a stale and an unread wallet: 2 + 1 = 3 left out by chain.
    expect(
      reconciliation({
        wallets: walletReadings(1, {
          stale: 1,
          unread: 1,
          failedChains: [failedChain('bitcoin', 2), failedChain('kaspa', 1)],
        }),
      }).wallets,
    ).toMatchObject({ compared: 1, stale: 1, unread: 1, chain_failed: 3 });
    // Every wallet left out: nothing is compared, so there is no oldest reading.
    expect(
      reconciliation({ wallets: walletReadings(0, { failedChains: [failedChain('kaspa', 4)] }) })
        .wallets,
    ).toEqual({
      compared: 0,
      stale: 0,
      unread: 0,
      chain_failed: 4,
      failed_chains: [{ chain_key: 'kaspa', wallets: 4 }],
      oldest_observed_at: null,
    });
    // A chain this build has no name for is still a chain the endpoint can list.
    expect(() =>
      reconciliation({
        wallets: walletReadings(0, { failedChains: [failedChain('litecoin', 1)] }),
      }),
    ).not.toThrow();
  });

  it('builds no entry and a count of zero when no chain failed', () => {
    expect(walletReadings(2)).toEqual({
      compared: 2,
      stale: 0,
      unread: 0,
      chain_failed: 0,
      failed_chains: [],
      oldest_observed_at: WALLETS_OBSERVED_AT,
    });
    expect(walletReadings(0, { stale: 1, unread: 2, failedChains: [] })).toEqual({
      compared: 0,
      stale: 1,
      unread: 2,
      chain_failed: 0,
      failed_chains: [],
      oldest_observed_at: null,
    });
    expect(NO_WALLETS).toEqual(walletReadings(0));
    expect(failedChain('kaspa', 3)).toEqual({ chain_key: 'kaspa', wallets: 3 });
  });

  it('refuses a chain_failed that is not what the failed chains add up to', () => {
    // Two wallets counted and one named.
    expect(() =>
      reconciliation({
        wallets: {
          ...NO_WALLETS,
          chain_failed: 2,
          failed_chains: [{ chain_key: 'bitcoin', wallets: 1 }],
        },
      }),
    ).toThrow('wallets.chain_failed is 2; the wallets of failed_chains add up to 1');
    // Counted, and no chain named: nothing would say which one.
    expect(() => reconciliation({ wallets: { ...NO_WALLETS, chain_failed: 1 } })).toThrow(
      'wallets.chain_failed is 1; the wallets of failed_chains add up to 0',
    );
    // Named, and not counted.
    expect(() =>
      reconciliation({
        wallets: { ...NO_WALLETS, failed_chains: [{ chain_key: 'kaspa', wallets: 3 }] },
      }),
    ).toThrow('wallets.chain_failed is 0; the wallets of failed_chains add up to 3');
    // Two chains: the total is their sum, not either of them.
    expect(() =>
      reconciliation({
        wallets: {
          ...NO_WALLETS,
          chain_failed: 2,
          failed_chains: [
            { chain_key: 'bitcoin', wallets: 2 },
            { chain_key: 'kaspa', wallets: 1 },
          ],
        },
      }),
    ).toThrow('wallets.chain_failed is 2; the wallets of failed_chains add up to 3');
  });

  it('refuses a failed chain with no wallet left out: only chains with one are listed', () => {
    expect(() =>
      reconciliation({
        wallets: { ...NO_WALLETS, failed_chains: [{ chain_key: 'bitcoin', wallets: 0 }] },
      }),
    ).toThrow('lists only the chains with a wallet left out, and bitcoin is stated with 0');
    expect(() =>
      reconciliation({
        wallets: {
          ...NO_WALLETS,
          chain_failed: 1,
          failed_chains: [
            { chain_key: 'bitcoin', wallets: 2 },
            { chain_key: 'kaspa', wallets: -1 },
          ],
        },
      }),
    ).toThrow('kaspa is stated with -1');
    expect(() =>
      reconciliation({
        wallets: {
          ...NO_WALLETS,
          chain_failed: 1,
          failed_chains: [{ chain_key: 'bitcoin', wallets: 1.5 }],
        },
      }),
    ).toThrow('bitcoin is stated with 1.5');
  });

  it('refuses failed chains out of order, and a chain listed twice', () => {
    expect(() =>
      reconciliation({
        wallets: walletReadings(0, {
          failedChains: [failedChain('kaspa', 1), failedChain('bitcoin', 1)],
        }),
      }),
    ).toThrow('wallets.failed_chains lists one entry per chain, by chain_key');
    expect(() =>
      reconciliation({
        wallets: walletReadings(0, {
          failedChains: [failedChain('bitcoin', 1), failedChain('bitcoin', 1)],
        }),
      }),
    ).toThrow('one entry per chain');
  });

  it('refuses a chain_failed that is not a count', () => {
    expect(() => reconciliation({ wallets: { ...NO_WALLETS, chain_failed: -1 } })).toThrow(
      'non-negative integers',
    );
    expect(() => reconciliation({ wallets: { ...NO_WALLETS, chain_failed: 0.5 } })).toThrow(
      'non-negative integers',
    );
  });

  it('gives a wallet left out for its chain no part in the oldest reading, or in a quantity', () => {
    // Left out, so its reading bounds nothing that is compared.
    expect(() =>
      reconciliation({
        wallets: {
          ...walletReadings(0, { failedChains: [failedChain('bitcoin', 2)] }),
          oldest_observed_at: BALANCES_READ_AT,
        },
      }),
    ).toThrow('oldest_observed_at');
    // And it contributes nothing: a wallet quantity needs a wallet that is compared.
    expect(() =>
      reconciliation({
        assets: [assetReconciliation()],
        wallets: walletReadings(0, { failedChains: [failedChain('bitcoin', 2)] }),
      }),
    ).toThrow('a wallet quantity comes from a wallet that is compared');
    expect(() =>
      reconciliation({
        assets: [assetReconciliation()],
        wallets: walletReadings(1, { failedChains: [failedChain('kaspa', 2)] }),
      }),
    ).not.toThrow();
  });
});

/**
 * The default `fakeAccounting` serves, and the rule that holds a stated one to the positions
 * beside it.
 */
describe('the reconciliation that goes with a snapshot', () => {
  it('matches every held position to the last place, and compares no closed one', () => {
    const response = matchingReconciliation(investedPortfolio());

    expect(response.computed_at).toBe(COMPUTED_AT);
    // BGB and XRP hold nothing and are held nowhere: left out.
    expect(response.assets.map((entry) => [entry.asset, entry.status])).toEqual([
      ['BTC', 'match'],
      ['ETH', 'match'],
      ['KAS', 'match'],
      ['SOL', 'match'],
    ]);
    const eth = response.assets[1];
    expect(eth?.history_quantity).toBe('2.718281828459045235');
    expect(eth?.held_quantity).toBe('2.718281828459045235');
    expect(eth?.difference).toBe(ZERO);
    // Nothing is left out: one venue, read and compared, and no wallet stale, unread or on a
    // chain that failed.
    expect(response.exchanges).toEqual([exchangeBalances()]);
    expect(response.exchanges[0]?.not_compared_reason).toBeNull();
    expect(response.wallets).toEqual(NO_WALLETS);
    expect(response.wallets.chain_failed).toBe(0);
    expect(response.wallets.failed_chains).toEqual([]);
    expect(response.max_reading_age_hours).toBe(MAX_READING_AGE_HOURS);
    expect(response.max_reading_age_hours).toBe(24);
    // The recompute that wrote the snapshot, as the positions serve it.
    expect(response.last_recompute).toEqual(investedPortfolio().last_recompute);
    expect(response.last_recompute?.outcome).toBe('written');
  });

  it('carries the positions own last_recompute, a failed one included', () => {
    const stale = investedPortfolio({ last_recompute: failedRecompute() });

    expect(matchingReconciliation(stale).last_recompute).toEqual(failedRecompute());
    // The comparison is still served: hiding it is the page's job.
    expect(matchingReconciliation(stale).assets).toHaveLength(4);
    expect(matchingReconciliation(failedFirstRecompute())).toEqual(
      notReconciled({ exchanges: [], last_recompute: failedRecompute() }),
    );
    expect(matchingReconciliation(noSnapshot()).last_recompute).toBeNull();
  });

  it('refuses a last_recompute that is not the one the positions serve', () => {
    expect(() => {
      assertSameSnapshot(reconciliation({ last_recompute: failedRecompute() }), emptySnapshot());
    }).toThrow('and both read one status');
    expect(() => {
      assertSameSnapshot(reconciliation({ last_recompute: null }), emptySnapshot());
    }).toThrow('last_recompute is null');
    expect(() => {
      assertSameSnapshot(
        reconciliation({ last_recompute: lastRecompute({ outcome: 'unchanged' }) }),
        emptySnapshot(),
      );
    }).toThrow('and both read one status');
    expect(() => {
      assertSameSnapshot(
        reconciliation({ last_recompute: failedRecompute() }),
        emptySnapshot({ last_recompute: failedRecompute() }),
      );
    }).not.toThrow();
  });

  it('compares nothing and names no venue over a snapshot that holds nothing', () => {
    expect(matchingReconciliation(emptySnapshot())).toEqual(
      reconciliation({ assets: [], exchanges: [] }),
    );
  });

  it('is "not computed" when there is no snapshot', () => {
    const response = matchingReconciliation(noSnapshot());

    expect(response.computed_at).toBeNull();
    expect(response.assets).toEqual([]);
  });

  it('refuses a comparison of another snapshot than the positions', () => {
    expect(() => {
      assertSameSnapshot(reconciliation({ computed_at: '2026-09-24T11:00:00Z' }), emptySnapshot());
    }).toThrow("the positions' snapshot was computed at");
    expect(() => {
      assertSameSnapshot(notReconciled(), emptySnapshot());
    }).toThrow('computed_at is null');
  });

  it('refuses a history side that is not the position, a held position not compared, and a history with no position', () => {
    const btcOnly = positionsResponse({
      positions: [position()],
      totals: totals({
        total_invested: '52500.000000000000000000',
        market_value: '90000.000000000000000000',
        unrealized_pnl: '37500.000000000000000000',
        unrealized_return_pct: '71.4286',
        realized_pnl: '7500.000000000000000000',
      }),
    });

    // The position holds 1.5; the default BTC entry says the history holds 0.5.
    expect(() => {
      assertSameSnapshot(
        reconciliation({ assets: [assetReconciliation()], wallets: walletReadings(1) }),
        btcOnly,
      );
    }).toThrow('BTC history_quantity is 0.500000000000000000; the position holds 1.5');
    expect(() => {
      assertSameSnapshot(reconciliation(), btcOnly);
    }).toThrow('BTC is held in the positions');
    expect(() => {
      assertSameSnapshot(
        reconciliation({
          assets: [matchedAsset('BTC', '1.500000000000000000'), solOver()],
        }),
        btcOnly,
      );
    }).toThrow('SOL history_quantity is 10.000000000000000000, and it has no position');
  });

  it('accepts an asset held and never traded beside the positions', () => {
    expect(() => {
      assertSameSnapshot(
        reconciliation({ assets: [kasNeverTraded()], wallets: walletReadings(1) }),
        emptySnapshot(),
      );
    }).not.toThrow();
  });

  it('accepts the comparison written for the full portfolio, one asset of every kind', () => {
    const gaps = investedPortfolioGaps();

    expect(() => {
      assertSameSnapshot(gaps, investedPortfolio());
    }).not.toThrow();
    expect(gaps.assets.map((entry) => [entry.asset, entry.status])).toEqual([
      ['BTC', 'history_short'],
      ['ETH', 'history_short'],
      ['KAS', 'match'],
      ['SOL', 'history_over'],
      ['XRP', 'history_short'],
    ]);
    // The constants the page tests assert against are the fixture's own strings.
    expect(gaps.assets[0]).toMatchObject({
      history_quantity: BTC_BEYOND.history,
      wallet_quantity: BTC_BEYOND.wallets,
      exchange_quantity: BTC_BEYOND.exchanges,
      held_quantity: BTC_BEYOND.held,
      difference: BTC_BEYOND.difference,
    });
    expect(gaps.assets[1]).toMatchObject({
      history_quantity: ETH_BEYOND.history,
      exchange_quantity: ETH_BEYOND.exchanges,
      difference: ETH_BEYOND.difference,
    });
    expect(gaps.assets[4]).toMatchObject({
      history_quantity: XRP_LEFT.history,
      exchange_quantity: XRP_LEFT.exchanges,
      difference: XRP_LEFT.difference,
    });
  });

  it('accepts BTC read below its position, and its constants', () => {
    const below = reconciliation({ assets: [btcBelowPosition()] });

    expect(below.assets[0]).toMatchObject({
      history_quantity: BTC_BELOW.history,
      wallet_quantity: BTC_BELOW.wallets,
      exchange_quantity: BTC_BELOW.exchanges,
      held_quantity: BTC_BELOW.held,
      difference: BTC_BELOW.difference,
      status: 'history_over',
    });
  });
});

describe('fakeAccounting and the reconciliation', () => {
  it('serves the quiet default over its positions, and moves it with them', () => {
    const fake = fakeAccounting({ positions: breakEvenPortfolio() });
    expect(fake.reconciliation()).toEqual(matchingReconciliation(breakEvenPortfolio()));

    fake.setPositions(investedPortfolio());
    expect(fake.reconciliation()).toEqual(matchingReconciliation(investedPortfolio()));
  });

  it('refuses a stated reconciliation of another snapshot, at construction and afterwards', () => {
    expect(() =>
      fakeAccounting({ positions: noSnapshot(), reconciliation: reconciliation() }),
    ).toThrow('Impossible reconciliation fixture');

    const fake = fakeAccounting({ positions: breakEvenPortfolio() });
    expect(() => {
      fake.setReconciliation(reconciliation());
    }).toThrow('BTC is held in the positions');
    expect(() => {
      fake.setPositions(emptySnapshot(), matchingReconciliation(breakEvenPortfolio()));
    }).toThrow('and it has no position');
    // A refused pair replaces neither: the fake still serves what it served before.
    expect(fake.positions()).toEqual(breakEvenPortfolio());
    expect(fake.reconciliation()).toEqual(matchingReconciliation(breakEvenPortfolio()));
  });
});
