import { describe, expect, it } from 'vitest';

import { NO_FILTERS, type FillFilters } from '@/lib/fillFilters';
import {
  completenessNotes,
  COMPLETENESS_UNKNOWN,
  describeEmptyFills,
  describeFillScope,
  describePage,
  describeShowing,
  describeUnvaluedFills,
  emptyFillsWords,
  FILL_SIDE_LABELS,
  pageCount,
  selectedExchanges,
} from '@/lib/fills';
import {
  authFailedExchange,
  erroredExchange,
  exchange,
  TRUNCATED_EFFECTIVE_SINCE_TEXT,
  truncatedExchange,
  unsyncedExchange,
} from '@/test/exchangeFixtures';
import { ALL_FILL_SIDES } from '@/test/fillFixtures';
import { inTimeZone } from '@/test/timeZone';

/**
 * Spec 024's words for the transactions view, checked as pure functions. The page tests in
 * `pages/ExchangesTransactions.test.tsx` check that each one reaches the screen; these check
 * every branch of the rules behind them, where a mutation is cheapest to see.
 *
 * The completeness sentences are the spec's own, written out here: "Bitget history begins
 * on {effective_since}; nothing before it is held.", "Bitget's import has N windows still to
 * read.", "Bitget's last sync failed, so its latest trades may be missing."
 */

const MARCH: FillFilters = { exchanges: [], fromDay: '2026-03-01', toDay: '2026-03-31' };

const truncatedLine = (venue: string): string =>
  `${venue} history begins on ${TRUNCATED_EFFECTIVE_SINCE_TEXT}; nothing before it is held.`;
const pendingLine = (venue: string, count: number): string =>
  `${venue}'s import has ${String(count)} ${count === 1 ? 'window' : 'windows'} still to read.`;
const failingLine = (venue: string): string =>
  `${venue}'s last sync failed, so its latest trades may be missing.`;

describe('the side', () => {
  it.each(ALL_FILL_SIDES)('%s is a word, never only a colour', (side) => {
    expect(FILL_SIDE_LABELS[side]).toMatch(/^[A-Z][a-z]+$/);
  });

  it('buy and sell read differently', () => {
    expect(FILL_SIDE_LABELS.buy).toBe('Buy');
    expect(FILL_SIDE_LABELS.sell).toBe('Sell');
  });
});

describe('describeUnvaluedFills', () => {
  it.each([
    [1, '(1 fill not in USDT)'],
    [2, '(2 fills not in USDT)'],
    [1234, '(1,234 fills not in USDT)'],
  ])('says %i as %j', (count, expected) => {
    expect(describeUnvaluedFills(count)).toBe(expected);
  });
});

describe('selectedExchanges', () => {
  const list = [exchange({ exchange_key: 'bingx' }), exchange()];

  it('is every venue when none is chosen', () => {
    expect(selectedExchanges(list, NO_FILTERS)).toEqual(list);
  });

  it('is the chosen venues only', () => {
    expect(
      selectedExchanges(list, { ...NO_FILTERS, exchanges: ['bitget'] }).map((e) => e.exchange_key),
    ).toEqual(['bitget']);
  });

  it('is nothing for a chosen venue the list does not hold', () => {
    expect(selectedExchanges([exchange()], { ...NO_FILTERS, exchanges: ['bingx'] })).toEqual([]);
  });
});

describe('describeFillScope', () => {
  it('names the count, the venues and the inclusive days', () => {
    inTimeZone('Europe/Madrid');
    expect(describeFillScope(812, { ...MARCH, exchanges: ['bingx', 'bitget'] })).toBe(
      '812 fills on BingX and Bitget from Mar 1, 2026 to Mar 31, 2026 (inclusive).',
    );
  });

  it('says every exchange and any date with no filter', () => {
    expect(describeFillScope(1, NO_FILTERS)).toBe('1 fill on all exchanges at any date.');
    expect(describeFillScope(0, NO_FILTERS)).toBe('0 fills on all exchanges at any date.');
    expect(describeFillScope(1234, NO_FILTERS)).toBe('1,234 fills on all exchanges at any date.');
  });

  it('says an open-ended range as one', () => {
    expect(
      describeFillScope(3, { ...NO_FILTERS, exchanges: ['bitget'], fromDay: '2026-03-01' }),
    ).toBe('3 fills on Bitget from Mar 1, 2026 onward (inclusive).');
    expect(describeFillScope(3, { ...NO_FILTERS, toDay: '2026-03-31' })).toBe(
      '3 fills on all exchanges up to Mar 31, 2026 (inclusive).',
    );
  });
});

describe('pagination words', () => {
  it.each([
    [1, 812, 'Showing 1 to 5 of 812', 'Page 1 of 163'],
    [2, 812, 'Showing 6 to 10 of 812', 'Page 2 of 163'],
    [163, 812, 'Showing 811 to 812 of 812', 'Page 163 of 163'],
    [1, 5, 'Showing 1 to 5 of 5', 'Page 1 of 1'],
    [2, 6, 'Showing 6 to 6 of 6', 'Page 2 of 2'],
    [1, 1, 'Showing 1 to 1 of 1', 'Page 1 of 1'],
    [201, 1234, 'Showing 1,001 to 1,005 of 1,234', 'Page 201 of 247'],
  ])('page %i of %i fills', (page, total, showing, pageWords) => {
    expect(describeShowing(page, total)).toBe(showing);
    expect(describePage(page, total)).toBe(pageWords);
  });

  it.each([
    [1, 1],
    [4, 1],
    [5, 1],
    [6, 2],
    [10, 2],
    [11, 3],
    [812, 163],
  ])('%i fills make %i pages', (total, pages) => {
    expect(pageCount(total)).toBe(pages);
  });

  it('counts in another page size when given one, as the sync history does', () => {
    expect(describeShowing(2, 51, 50)).toBe('Showing 51 to 51 of 51');
    expect(describePage(2, 51, 50)).toBe('Page 2 of 2');
    expect(pageCount(51, 50)).toBe(2);
    expect(pageCount(50, 50)).toBe(1);
  });
});

describe('completenessNotes', () => {
  it('says completeness is unknown when the list could not be read', () => {
    expect(completenessNotes(undefined, NO_FILTERS)).toEqual([COMPLETENESS_UNKNOWN]);
    expect(COMPLETENESS_UNKNOWN).toBe(
      'Whether this history is complete is unknown: the exchange list could not be read.',
    );
  });

  it('says nothing for a venue that holds its whole history and synced', () => {
    expect(completenessNotes([exchange(), unsyncedExchange('bingx')], NO_FILTERS)).toEqual([]);
  });

  it('names a truncated venue and where what is held begins', () => {
    expect(completenessNotes([truncatedExchange()], NO_FILTERS)).toEqual([truncatedLine('Bitget')]);
  });

  it('says more when the selected range starts before what is held', () => {
    inTimeZone('UTC');
    // effective_since is 27 June 12:05:36.25 UTC. A range from 27 June starts at midnight,
    // before it; one from 28 June does not.
    const stronger =
      "The selected range starts before Bitget's history begins on " +
      `${TRUNCATED_EFFECTIVE_SINCE_TEXT}. Nothing before that date is held.`;

    expect(
      completenessNotes([truncatedExchange()], { ...NO_FILTERS, fromDay: '2026-06-27' }),
    ).toEqual([stronger]);
    expect(
      completenessNotes([truncatedExchange()], { ...NO_FILTERS, fromDay: '2026-06-28' }),
    ).toEqual([truncatedLine('Bitget')]);
    // An end day alone does not start the range anywhere.
    expect(
      completenessNotes([truncatedExchange()], { ...NO_FILTERS, toDay: '2026-06-27' }),
    ).toEqual([truncatedLine('Bitget')]);
  });

  it('compares the local start of the day, not the UTC one', () => {
    // At UTC+14, 28 June begins at 27 June 10:00 UTC: before effective_since (12:05:36.25
    // UTC), so the range does start before what is held. Read as UTC midnight, 28 June would
    // start after it, and the stronger sentence would be lost.
    inTimeZone('Pacific/Kiritimati');
    expect(
      completenessNotes([truncatedExchange()], { ...NO_FILTERS, fromDay: '2026-06-28' })[0],
    ).toMatch(/^The selected range starts before/);
  });

  it('a day that begins after what is held gets the plain sentence, in any zone', () => {
    // In Tokyo, 28 June begins at 27 June 15:00 UTC, after effective_since.
    inTimeZone('Asia/Tokyo');
    expect(
      completenessNotes([truncatedExchange()], { ...NO_FILTERS, fromDay: '2026-06-28' }),
    ).toEqual([truncatedLine('Bitget')]);
  });

  it('names windows still to read, singular and plural', () => {
    expect(completenessNotes([exchange({ pending_windows: 1 })], NO_FILTERS)).toEqual([
      pendingLine('Bitget', 1),
    ]);
    expect(
      completenessNotes([exchange({ exchange_key: 'bingx', pending_windows: 4 })], NO_FILTERS),
    ).toEqual([pendingLine('BingX', 4)]);
  });

  it.each([
    ['error', erroredExchange('unavailable')],
    ['auth_failed', authFailedExchange('auth')],
  ])('names a %s venue as failing, after its other reasons', (_status, entry) => {
    // Both fixtures hold queued windows, as the backend writes them; the refused key's is
    // also truncated. One sentence per reason, in a fixed order.
    const notes = completenessNotes([entry], NO_FILTERS);

    expect(notes.at(-1)).toBe(failingLine('Bitget'));
    expect(notes).toContain(pendingLine('Bitget', entry.pending_windows));
    expect(notes).toHaveLength(entry.history_truncated ? 3 : 2);
  });

  it('speaks only of the venues selected', () => {
    const list = [erroredExchange('unavailable', { exchange_key: 'bingx' }), truncatedExchange()];

    expect(completenessNotes(list, { ...NO_FILTERS, exchanges: ['bitget'] })).toEqual([
      truncatedLine('Bitget'),
    ]);
    expect(completenessNotes(list, NO_FILTERS)).toEqual([
      pendingLine('BingX', 2),
      failingLine('BingX'),
      truncatedLine('Bitget'),
    ]);
  });
});

describe('describeEmptyFills: the first that holds wins', () => {
  const failing = erroredExchange('unavailable');
  const healthy = exchange({ exchange_key: 'bingx' });

  it('1. a selected venue failing outranks everything, filters included', () => {
    expect(describeEmptyFills(NO_FILTERS, [healthy, failing])).toEqual({
      kind: 'sync_failing',
      venues: ['bitget'],
    });
    expect(describeEmptyFills(MARCH, [healthy, failing])).toEqual({
      kind: 'sync_failing',
      venues: ['bitget'],
    });
    expect(
      describeEmptyFills(NO_FILTERS, [
        authFailedExchange('auth', { exchange_key: 'bingx' }),
        failing,
      ]),
    ).toEqual({ kind: 'sync_failing', venues: ['bingx', 'bitget'] });
  });

  it('a failing venue that is not selected does not count', () => {
    expect(describeEmptyFills({ ...NO_FILTERS, exchanges: ['bingx'] }, [healthy, failing])).toEqual(
      { kind: 'no_match' },
    );
  });

  it('2. with no filter and nothing failing, nothing is imported yet', () => {
    expect(describeEmptyFills(NO_FILTERS, [unsyncedExchange('bingx'), unsyncedExchange()])).toEqual(
      { kind: 'none_imported' },
    );
    // The list could not be read: whether a venue fails is unknown, and is not guessed.
    expect(describeEmptyFills(NO_FILTERS, undefined)).toEqual({ kind: 'none_imported' });
  });

  it('3. with a filter set, nothing matches it', () => {
    expect(describeEmptyFills(MARCH, [healthy])).toEqual({ kind: 'no_match' });
    expect(describeEmptyFills({ ...NO_FILTERS, toDay: '2026-03-01' }, undefined)).toEqual({
      kind: 'no_match',
    });
    expect(describeEmptyFills({ ...NO_FILTERS, exchanges: ['bingx'] }, [healthy])).toEqual({
      kind: 'no_match',
    });
  });

  it('4. otherwise, nothing is imported yet', () => {
    // The list holds fills and the query matched none with no filter: a sync committing
    // between the two reads.
    expect(describeEmptyFills(NO_FILTERS, [healthy])).toEqual({ kind: 'none_imported' });
  });

  it('gives each state its title', () => {
    expect(emptyFillsWords({ kind: 'sync_failing', venues: ['bitget'] }).title).toBe(
      'The last sync is failing',
    );
    expect(
      emptyFillsWords({ kind: 'sync_failing', venues: ['bingx', 'bitget'] }).description,
    ).toContain('BingX and Bitget');
    expect(emptyFillsWords({ kind: 'no_match' }).title).toBe('Nothing matches these filters');
    expect(emptyFillsWords({ kind: 'none_imported' }).title).toBe('No fills imported yet');
  });
});
