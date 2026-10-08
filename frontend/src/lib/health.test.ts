import { describe, expect, it } from 'vitest';

import {
  CHAIN_STATE_WORDS,
  CHAINS_EMPTY_WORDS,
  NEVER_WORDS,
  NO_TICK_WORDS,
  PRICES_STATE_WORDS,
  TICK_FAILED_WORDS,
  TICK_SUCCEEDED_WORDS,
  TIMER_NAMES,
  TIMER_STATE_WORDS,
  UNAVAILABLE_WORDS,
} from '@/lib/health';

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
      'price-backfill',
      'balance-rebuild',
      'backup',
    ]);
    expect(Object.keys(TIMER_STATE_WORDS)).toEqual(['ok', 'late', 'stopped', 'disabled']);
    expect(Object.keys(CHAIN_STATE_WORDS)).toEqual(['ok', 'failing', 'never']);
    expect(Object.keys(PRICES_STATE_WORDS)).toEqual(['fresh', 'stale', 'never']);
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
      ...Object.values(TIMER_STATE_WORDS),
      ...Object.values(CHAIN_STATE_WORDS),
      ...Object.values(PRICES_STATE_WORDS),
    ];

    expect(new Set(sentences).size).toBe(sentences.length);
  });

  it('opens every state sentence with its label, so it reads without colour', () => {
    const tables = [TIMER_STATE_WORDS, CHAIN_STATE_WORDS, PRICES_STATE_WORDS];

    for (const table of tables) {
      for (const sentence of Object.values(table)) {
        expect(sentence).toMatch(/^[A-Z][A-Za-z]+( [a-z]+)?\. /u);
      }
    }
  });
});
