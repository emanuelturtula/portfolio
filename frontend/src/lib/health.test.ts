import { describe, expect, it } from 'vitest';

import {
  CHAIN_STATE_WORDS,
  CHAINS_EMPTY_WORDS,
  countWords,
  EXCHANGE_BALANCES_WORDS,
  EXCHANGE_SYNC_WORDS,
  exchangeName,
  EXCHANGES_EMPTY_WORDS,
  NEVER_WORDS,
  NO_TICK_WORDS,
  PRICES_STATE_WORDS,
  RECONCILIATION_LINK_WORDS,
  RECONCILIATION_STATE_WORDS,
  reconciliationCounts,
  TICK_FAILED_WORDS,
  TICK_SUCCEEDED_WORDS,
  TIMER_NAMES,
  TIMER_STATE_WORDS,
  UNAVAILABLE_WORDS,
  UNKNOWN_COUNT_WORDS,
} from '@/lib/health';
import { reconciliation } from '@/test/healthFixtures';

/**
 * The words of the Health page's sections after Backups (spec 030), without React. The tables
 * are total by type; these say which keys they have, so a state dropped on the backend and
 * left here shows up as a diff, and that no two meanings share words.
 */
describe('the health words', () => {
  it('has words for exactly the states each table is keyed by', () => {
    expect(Object.keys(TIMER_NAMES)).toEqual([
      'balance-sync',
      'price-refresh',
      'exchange-sync',
      'backup',
    ]);
    expect(Object.keys(TIMER_STATE_WORDS)).toEqual(['ok', 'late', 'stopped', 'disabled']);
    expect(Object.keys(CHAIN_STATE_WORDS)).toEqual(['ok', 'failing', 'never']);
    expect(Object.keys(EXCHANGE_SYNC_WORDS)).toEqual([
      'ok',
      'error',
      'auth_failed',
      'never_synced',
    ]);
    expect(Object.keys(EXCHANGE_BALANCES_WORDS)).toEqual(['ok', 'failing', 'never']);
    expect(Object.keys(PRICES_STATE_WORDS)).toEqual(['fresh', 'stale', 'never']);
    expect(Object.keys(RECONCILIATION_STATE_WORDS)).toEqual([
      'match',
      'mismatch',
      'incomplete',
      'not_computed',
    ]);
  });

  it('never words two meanings alike', () => {
    const sentences = [
      UNAVAILABLE_WORDS,
      NEVER_WORDS,
      NO_TICK_WORDS,
      TICK_SUCCEEDED_WORDS,
      TICK_FAILED_WORDS,
      CHAINS_EMPTY_WORDS.title,
      CHAINS_EMPTY_WORDS.description,
      EXCHANGES_EMPTY_WORDS.title,
      EXCHANGES_EMPTY_WORDS.description,
      RECONCILIATION_LINK_WORDS,
      UNKNOWN_COUNT_WORDS,
      ...Object.values(TIMER_STATE_WORDS),
      ...Object.values(CHAIN_STATE_WORDS),
      ...Object.values(EXCHANGE_SYNC_WORDS),
      ...Object.values(EXCHANGE_BALANCES_WORDS),
      ...Object.values(PRICES_STATE_WORDS),
      ...Object.values(RECONCILIATION_STATE_WORDS),
    ];

    expect(new Set(sentences).size).toBe(sentences.length);
  });

  it('opens every state sentence with its label, so it reads without colour', () => {
    const tables = [
      TIMER_STATE_WORDS,
      CHAIN_STATE_WORDS,
      EXCHANGE_SYNC_WORDS,
      EXCHANGE_BALANCES_WORDS,
      PRICES_STATE_WORDS,
      RECONCILIATION_STATE_WORDS,
    ];

    for (const table of tables) {
      for (const sentence of Object.values(table)) {
        expect(sentence).toMatch(/^[A-Z][A-Za-z]+( [a-z]+)?\. /u);
      }
    }
  });

  it('names each exchange', () => {
    expect(exchangeName('bitget')).toBe('Bitget');
    expect(exchangeName('bingx')).toBe('BingX');
  });
});

describe('countWords', () => {
  it('says "unknown" for a count the section did not serve', () => {
    expect(countWords(null)).toBe('unknown');
    expect(UNKNOWN_COUNT_WORDS).toBe('unknown');
  });

  it('groups a count, and shows zero as zero', () => {
    expect(countWords(0)).toBe('0');
    expect(countWords(7)).toBe('7');
    expect(countWords(1234567)).toBe('1,234,567');
  });
});

describe('reconciliationCounts', () => {
  it('gives three labelled counts for a computed check', () => {
    expect(
      reconciliationCounts(
        reconciliation({ assets_compared: 12, assets_mismatched: 3, sources_not_compared: 1 }),
      ),
    ).toEqual([
      { label: 'Assets compared', value: '12' },
      { label: 'Assets that differ', value: '3' },
      { label: 'Sources not compared', value: '1' },
    ]);
  });

  it.each(['mismatch', 'incomplete'] as const)('gives the counts for %s too', (state) => {
    expect(reconciliationCounts(reconciliation({ state }))).toHaveLength(3);
  });

  it('says "unknown" for a count that is missing from a computed check', () => {
    expect(
      reconciliationCounts(
        reconciliation({ assets_compared: null, assets_mismatched: null, sources_not_compared: 2 }),
      ).map(({ value }) => value),
    ).toEqual(['unknown', 'unknown', '2']);
  });

  it('gives no count for a check never computed, nor for one that could not be read', () => {
    expect(reconciliationCounts(reconciliation({ state: 'not_computed' }))).toEqual([]);
    expect(
      reconciliationCounts(
        reconciliation({
          state: 'unavailable',
          computed_at: null,
          assets_compared: null,
          assets_mismatched: null,
          sources_not_compared: null,
        }),
      ),
    ).toEqual([]);
  });
});
