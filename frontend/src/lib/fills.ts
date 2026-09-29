/**
 * The transactions view's pure logic: the words for a fill's side, the sentence that states
 * a scope, what the history is known to be missing, and which of the three empty states
 * applies. See docs/specs/024-exchange-transactions.md.
 *
 * No React anywhere in this module - it is exercised directly by tests and by the components
 * under `src/pages/exchanges/`, the same split `lib/accounting.ts` makes for the dashboard.
 *
 * No amount is computed here. Every figure on the page comes from the server, as a string,
 * and is formatted by `<Money>`; the client sums nothing.
 */
import type { Exchange, FillSide } from '@/api/exchanges';
import { formatVenues } from '@/lib/accounting';
import {
  EXCHANGES,
  formatCount,
  hasFailedSync,
  isTruncated,
  type ExchangeKey,
} from '@/lib/exchanges';
import {
  dayRangeToInstants,
  FILLS_PAGE_SIZE,
  formatDayLabel,
  hasActiveFilters,
  type FillFilters,
} from '@/lib/fillFilters';
import type { FormatMoneyOptions } from '@/lib/money';
import { formatHistoryStart, parseInstant } from '@/lib/time';

/**
 * Net quantities: the sign is a `+` or `-` in the text, zero unsigned, and up to 8 places
 * like every other quantity. A net is buys minus sells, so it can be negative over a filtered
 * range, and a sign carried by colour alone would not say so.
 */
export const SIGNED_QUANTITY_FORMAT: FormatMoneyOptions = { signDisplay: 'exceptZero' };

/**
 * The side is a word. A colour would be a second channel, never the only one.
 * `Record` over the generated union, so a side added on the backend fails `tsc` until it has
 * a word.
 */
export const FILL_SIDE_LABELS: Record<FillSide, string> = {
  buy: 'Buy',
  sell: 'Sell',
};

/** An order id the venue did not send: said in words, never an empty cell. */
export const NO_ORDER_ID = 'none';

/** A fill with no fee asset is a zero fee (the backend writes a null asset only then). */
export const NO_FEE = 'None';

/** The USDT value of a fill quoted in something else, which is never converted. */
export const NOT_IN_USDT = 'Not in USDT';

/** Beside a quote value the venue did not send, so it was derived from quantity and price. */
export const DERIVED_MARKER = '(derived)';
export const DERIVED_LEGEND =
  '(derived): the venue did not report this quote value, so it was worked out from the ' +
  'quantity and the price.';

export const COMPLETENESS_UNKNOWN =
  'Whether this history is complete is unknown: the exchange list could not be read.';

export const INVERTED_RANGE_MESSAGE =
  'The end day is before the start day, so nothing was requested.';

/** Every net is buys minus sells (spec 024), so a positive one is net buying. */
export const NET_LEGEND =
  'Net is bought minus sold, and USDT net is spent minus received, so a positive figure ' +
  'means net buying. Over a filtered range either can be negative.';

/** Fees are signed sums per asset. The sign is the only thing that tells a rebate. */
export const FEES_LEGEND = 'Positive is a fee paid, negative is a rebate. Never converted.';

function plural(count: number, noun: string): string {
  return `${formatCount(count)} ${noun}${count === 1 ? '' : 's'}`;
}

/** "(3 fills not in USDT)": how many of an asset's fills its USDT figures leave out. */
export function describeUnvaluedFills(count: number): string {
  return `(${plural(count, 'fill')} not in USDT)`;
}

/**
 * The venues a filter selects, out of the ones the exchange list holds. No exchange chosen is
 * every exchange, as the API reads an omitted `exchange`.
 */
export function selectedExchanges(
  exchanges: readonly Exchange[],
  filters: FillFilters,
): readonly Exchange[] {
  return filters.exchanges.length === 0
    ? exchanges
    : exchanges.filter((exchange) => filters.exchanges.includes(exchange.exchange_key));
}

function describeDays({ fromDay, toDay }: FillFilters): string {
  if (fromDay !== null && toDay !== null) {
    return `from ${formatDayLabel(fromDay)} to ${formatDayLabel(toDay)} (inclusive)`;
  }
  if (fromDay !== null) {
    return `from ${formatDayLabel(fromDay)} onward (inclusive)`;
  }
  if (toDay !== null) {
    return `up to ${formatDayLabel(toDay)} (inclusive)`;
  }
  return 'at any date';
}

/**
 * The sentence above the totals that says what they cover: how many fills, on which venues,
 * over which days. "812 fills on BingX and Bitget from Mar 1, 2026 to Mar 31, 2026
 * (inclusive)." The end day is inclusive here, as the owner picked it; the API's own bound
 * is exclusive and is not what is shown.
 */
export function describeFillScope(totalCount: number, filters: FillFilters): string {
  const venues = filters.exchanges.length === 0 ? 'all exchanges' : formatVenues(filters.exchanges);
  return `${plural(totalCount, 'fill')} on ${venues} ${describeDays(filters)}.`;
}

/** "Showing 51 to 100 of 812", for the 1-based `page` of `total` fills. */
export function describeShowing(page: number, total: number): string {
  const first = (page - 1) * FILLS_PAGE_SIZE + 1;
  const last = Math.min(page * FILLS_PAGE_SIZE, total);
  return `Showing ${formatCount(first)} to ${formatCount(last)} of ${formatCount(total)}`;
}

/** How many pages `total` fills make. Only asked when there are some. */
export function pageCount(total: number): number {
  return Math.ceil(total / FILLS_PAGE_SIZE);
}

/** "Page 2 of 17". */
export function describePage(page: number, total: number): string {
  return `Page ${formatCount(page)} of ${formatCount(pageCount(total))}`;
}

/**
 * What one venue's row in the exchange list says about the completeness of what is imported:
 * a retention cut, a backfill still running, a failing sync. `rangeStart` is the millisecond
 * instant the selected range starts at, or `null` for one with no start.
 */
function venueNotes(exchange: Exchange, rangeStart: number | null): string[] {
  const venue = EXCHANGES[exchange.exchange_key].name;
  const notes: string[] = [];

  if (isTruncated(exchange)) {
    const since = formatHistoryStart(exchange.effective_since);
    // A range that starts before what is held is a stronger statement than a history that
    // merely begins late: it says the days the owner asked about are, in part, absent.
    notes.push(
      rangeStart !== null && rangeStart < parseInstant(exchange.effective_since)
        ? `The selected range starts before ${venue}'s history begins on ${since}. Nothing ` +
            'before that date is held.'
        : `${venue} history begins on ${since}; nothing before it is held.`,
    );
  }
  if (exchange.pending_windows > 0) {
    notes.push(
      `${venue}'s import has ${plural(exchange.pending_windows, 'window')} still to read.`,
    );
  }
  if (hasFailedSync(exchange)) {
    notes.push(`${venue}'s last sync failed, so its latest trades may be missing.`);
  }

  return notes;
}

/**
 * The reasons the totals may cover less than the venue holds, one sentence per selected venue
 * and reason (spec 024, and the issue's "Completeness"). A partial history read as complete is
 * the failure this exists to prevent, so an exchange list that could not be read is itself a
 * sentence: nothing can be said about completeness, and silence would read as "complete".
 */
export function completenessNotes(
  exchanges: readonly Exchange[] | undefined,
  filters: FillFilters,
): string[] {
  if (exchanges === undefined) {
    return [COMPLETENESS_UNKNOWN];
  }

  const { from } = dayRangeToInstants(filters.fromDay, null);
  const rangeStart = from === null ? null : parseInstant(from);

  return selectedExchanges(exchanges, filters).flatMap((exchange) =>
    venueNotes(exchange, rangeStart),
  );
}

/**
 * Which of the three empty states applies to a query that matched nothing. A discriminated
 * union rather than a string, so each component can only read the fields its own state
 * carries.
 *
 * The first that holds wins, and a failure outranks an absence: "no fills" and "the sync is
 * failing" look identical on screen and mean opposite things.
 *
 * 1. A selected venue's last sync failed: the fills may be missing, not absent.
 * 2. A filter is set: something narrower than everything matched nothing.
 * 3. Otherwise no fill has been imported yet.
 *
 * Spec 024 lists "no filters, and every venue stores 0 fills, or the list is unavailable"
 * apart from "otherwise", but both render "No fills imported yet", so the list's counts
 * change nothing here and are not read.
 *
 * `exchanges === undefined` is a list that could not be read, where row 1 cannot be decided
 * and is skipped rather than guessed.
 */
export type EmptyFills =
  | { readonly kind: 'sync_failing'; readonly venues: readonly ExchangeKey[] }
  | { readonly kind: 'no_match' }
  | { readonly kind: 'none_imported' };

export function describeEmptyFills(
  filters: FillFilters,
  exchanges: readonly Exchange[] | undefined,
): EmptyFills {
  const failing =
    exchanges === undefined
      ? []
      : selectedExchanges(exchanges, filters)
          .filter(hasFailedSync)
          .map((exchange) => exchange.exchange_key);

  if (failing.length > 0) {
    return { kind: 'sync_failing', venues: failing };
  }

  return hasActiveFilters(filters) ? { kind: 'no_match' } : { kind: 'none_imported' };
}

/** The title and sentence of each empty state. */
export function emptyFillsWords(state: EmptyFills): {
  readonly title: string;
  readonly description: string;
} {
  switch (state.kind) {
    case 'sync_failing':
      return {
        title: 'The last sync is failing',
        description:
          `No fills are listed, but the last sync failed for ${formatVenues(state.venues)}, ` +
          'so trades may be missing rather than absent.',
      };
    case 'no_match':
      return {
        title: 'Nothing matches these filters',
        description: 'No imported fill matches the selected exchanges and days.',
      };
    case 'none_imported':
      return {
        title: 'No fills imported yet',
        description: 'Trades appear here once an exchange sync has imported them.',
      };
  }
}
