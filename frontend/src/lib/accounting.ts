/**
 * The invested-per-asset dashboard's pure logic: which positions are held, which words go
 * with each data-quality flag and exclusion, and which of the empty states applies. See
 * docs/specs/022-invested-per-asset-dashboard.md.
 *
 * No React anywhere in this module - it is exercised directly by tests and by every
 * component under `src/pages/dashboard/`, the same split `lib/exchanges.ts` and
 * `lib/freshness.ts` use for the same reason.
 *
 * Every `Record` below is keyed by a generated union, so a member added to `PositionFlag`,
 * `ExclusionReason`, `PriceUnavailable` or `ValueUnavailable` on the backend fails `tsc`
 * here until it has words. A flag nobody can read the meaning of is a data-quality warning
 * that does not warn.
 */
import type { Exclusion, Position, Positions } from '@/api/accounting';
import type { Exchange } from '@/api/exchanges';
import type { components } from '@/api/generated/schema';
import { EXCHANGES, hasFailedSync, type ExchangeKey } from '@/lib/exchanges';
import { equalsMoney, isZeroMoney, money, type FormatMoneyOptions } from '@/lib/money';
import { PRICE_UNAVAILABLE_MESSAGES, type PriceUnavailable } from '@/lib/prices';

export type PositionFlag = components['schemas']['PositionFlag'];
export type ExclusionReason = components['schemas']['ExclusionReason'];
export type ValueUnavailable = components['schemas']['ValueUnavailable'];

/** Invested, market value and fees: an amount a person reads at face value, to the cent. */
export const AMOUNT_FORMAT: FormatMoneyOptions = {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
};

/**
 * Average cost and price. A unit price is not a value: KAS at 0.084912345678 rounds to
 * "0.08" under {@link AMOUNT_FORMAT}, and a value built from that rounded price no longer
 * multiplies out to what "Market value" shows. Same range as the value section's prices.
 */
export const UNIT_PRICE_FORMAT: FormatMoneyOptions = {
  minimumFractionDigits: 2,
  maximumFractionDigits: 8,
};

/**
 * Profit, loss and return: to the cent, and signed. The sign is a symbol in the text - `+`
 * or `-`, zero unsigned - because a gain and a loss must be distinguishable without colour.
 */
export const SIGNED_FORMAT: FormatMoneyOptions = {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: 'exceptZero',
};

/** The text on the badge beside an asset's symbol. Text, never an icon alone. */
export const FLAG_BADGES: Record<PositionFlag, string> = {
  history_incomplete: 'History incomplete',
  unattributed_fee: 'Fee not valued',
  unknown_basis: 'Unknown cost',
};

/**
 * What each flag means for the figures beside it, printed as a legend under the table for
 * every flag shown anywhere - on a row, or beside an asset in the line for those no longer
 * held.
 *
 * `unknown_basis` names the origins the engine writes today: units that arrived without a
 * cost. Selling units bought before the imported history begins is `history_incomplete`,
 * not this. Manual adjustments (#18) will add one - an adjustment entered without a cost -
 * and the sentence gains it when they can be entered.
 */
export const FLAG_EXPLANATIONS: Record<PositionFlag, string> = {
  history_incomplete:
    'A sale, or a fee paid in this asset, was larger than everything the imported history ' +
    'held, so a buy or a deposit is missing. The realized P&L of that disposal is ' +
    'unreliable, and this marker stays.',
  unattributed_fee:
    "A fee on this asset's trades was paid in another asset whose cost is unknown, so this " +
    "asset's figures leave that fee out.",
  unknown_basis:
    'Part of the holding arrived without a known cost: a swap paid for with units of ' +
    'unknown cost, or a fee rebate in an asset that is not cash. Its average cost, invested ' +
    'and unrealized P&L cover only the part with a known cost, while its market value ' +
    'covers every unit. It is left out of the portfolio totals.',
};

/**
 * Why a position is left out of the portfolio totals, worded to follow "{assets}:" - one
 * sentence for however many assets share the reason, hence no "it" or "they" in it. It
 * starts in lower case for the same reason.
 */
export const EXCLUSION_REASON_MESSAGES: Record<ExclusionReason, string> = {
  unknown_basis:
    'part of the holding has no known cost, so its value and its cost describe different ' +
    'quantities.',
  unpriced: 'no market value is available, so the cost has nothing to be compared with.',
};

/**
 * The sentence in the market value cell when there is no value: the price reason's own
 * sentence, or the one reason the valuation itself can add. The price sentences are
 * `PRICE_UNAVAILABLE_MESSAGES`', not copies of them, so the two pages cannot drift.
 */
export const MARKET_VALUE_UNAVAILABLE_MESSAGES: Record<
  PriceUnavailable | ValueUnavailable,
  string
> = {
  ...PRICE_UNAVAILABLE_MESSAGES,
  value_out_of_range: 'The value is too large to show.',
};

/**
 * Whether the position holds anything. Compared as a decimal, never as a string: the wire
 * sends a position with nothing left as `"0.000000000000000000"`, which is not `"0"`.
 */
export function isHeld(position: Position): boolean {
  return !isZeroMoney(money(position.quantity));
}

/**
 * Whether none of a held position's units has a known cost: `quantity` equals
 * `unknown_basis_quantity`. Its total invested is then `0`, which beside a market value would
 * read as a break-even rather than as "there is no cost to compare with", so the table shows
 * "—" for its invested and unrealized P&L. Compared as decimals, never as strings.
 */
export function hasNoKnownCost(position: Position): boolean {
  return equalsMoney(money(position.quantity), money(position.unknown_basis_quantity));
}

/**
 * The flags carried by any of `positions`, each once, alphabetical - like the backend's own
 * order for one position's flags, so the legend does not reshuffle when the first position
 * that carries a flag changes.
 */
export function flagsOf(positions: readonly Position[]): PositionFlag[] {
  return Array.from(new Set(positions.flatMap((position) => position.flags))).sort();
}

/**
 * The flags that make a position's realized P&L unreliable. `unknown_basis` is not one:
 * sales of units of unknown cost are kept out of realized P&L (`unmatched_proceeds`), so
 * they do not make it wrong.
 */
const UNRELIABLE_REALIZED_FLAGS: readonly PositionFlag[] = [
  'history_incomplete',
  'unattributed_fee',
];

/**
 * The assets - held or not - whose realized P&L rests on a history that is missing
 * something. Realized P&L is summed over every position, closed ones included, so a caveat
 * that looked only at the table would miss the asset that carries the flag and is not in it.
 */
export function assetsWithUnreliableRealizedPnl(positions: readonly Position[]): string[] {
  return positions
    .filter((position) => position.flags.some((flag) => UNRELIABLE_REALIZED_FLAGS.includes(flag)))
    .map((position) => position.asset);
}

/**
 * One reason per group, in the order the reasons first appear, with the assets that share
 * it in the endpoint's order. Most positions are unpriced (only chain assets have prices),
 * so listing each with the same sentence would print it a dozen times.
 */
export function groupExclusions(
  excluded: readonly Exclusion[],
): { readonly reason: ExclusionReason; readonly assets: readonly string[] }[] {
  const reasons = Array.from(new Set(excluded.map((entry) => entry.reason)));

  return reasons.map((reason) => ({
    reason,
    assets: excluded.filter((entry) => entry.reason === reason).map((entry) => entry.asset),
  }));
}

const ONE_CLOSED = { noun: 'asset', verb: 'is', possessive: 'Its' };
const MANY_CLOSED = { noun: 'assets', verb: 'are', possessive: 'Their' };

/**
 * The one line that stands for every asset no longer held that the table leaves out, naming
 * each, with the label of every flag it carries in brackets beside it: "2 assets no longer
 * held are not listed: BTC (History incomplete), ETH. Their realized P&L is in the total."
 *
 * The flags are named here because `history_incomplete` and `unattributed_fee` are sticky:
 * `dispose` empties the pool when a disposal exceeds it, so the asset with the worst history
 * is often exactly the one that has no row.
 *
 * "No longer held", not "fully sold": a fee paid in an asset the history never held leaves a
 * position at zero without a sale ever having happened.
 */
export function describeClosedPositions(closed: readonly Position[]): string {
  const { noun, verb, possessive } = closed.length === 1 ? ONE_CLOSED : MANY_CLOSED;
  const labels = closed.map((position) =>
    position.flags.length === 0
      ? position.asset
      : `${position.asset} (${position.flags.map((flag) => FLAG_BADGES[flag]).join(', ')})`,
  );

  return (
    `${String(closed.length)} ${noun} no longer held ${verb} not listed: ` +
    `${labels.join(', ')}. ${possessive} realized P&L is in the total.`
  );
}

/** "BingX", "BingX and Bitget" - a list of names as a person would say it. */
const LIST_FORMAT = new Intl.ListFormat('en', { style: 'long', type: 'conjunction' });

export function formatList(items: readonly string[]): string {
  return LIST_FORMAT.format(items);
}

/** The display names of `venues` as one phrase: "BingX", or "BingX and Bitget". */
export function formatVenues(venues: readonly ExchangeKey[]): string {
  return formatList(venues.map((venue) => EXCHANGES[venue].name));
}

function isExchangeKey(value: string): value is ExchangeKey {
  return Object.hasOwn(EXCHANGES, value);
}

/**
 * A venue's display name from the `source` a warning carries, or the raw `source` when it
 * is not one this application knows - a name we do not have is still better shown than
 * dropped.
 */
export function venueLabel(source: string): string {
  return isExchangeKey(source) ? EXCHANGES[source].name : source;
}

/**
 * The venues whose last sync failed - `error` or `auth_failed` - in the list's own order.
 * A venue that has never synced, or is syncing, has not failed. The rule itself is
 * `hasFailedSync`, so the dashboard's failing-sync checks and the transactions view's cannot drift.
 */
export function venuesWithFailedSync(exchanges: readonly Exchange[]): ExchangeKey[] {
  return exchanges.filter(hasFailedSync).map((exchange) => exchange.exchange_key);
}

/**
 * Which of the five empty states applies, when `positions` is empty. A discriminated union
 * rather than a string, so each component can only read the fields its own state carries.
 */
export type EmptyPositions =
  | { readonly kind: 'recompute_failed'; readonly at: string; readonly error: string | null }
  | { readonly kind: 'sync_failed'; readonly venues: readonly ExchangeKey[] }
  | { readonly kind: 'no_trades'; readonly anyConfigured: boolean | undefined }
  | { readonly kind: 'not_computed' }
  | { readonly kind: 'no_positions' };

/**
 * Tells an empty response apart from the four ways it can happen, first match wins:
 *
 * 1. the last recompute failed - ours, and the snapshot served is the one before it;
 * 2. a venue's last sync failed - trades may be missing, which is the opposite of "none";
 * 3. no trades - either the exchanges list is known, no venue has stored a fill and the
 *    snapshot does not contradict it (none written, or one that replayed nothing), or the
 *    list is unknown and the snapshot itself proves it: `computed_at` is set and
 *    `event_count` is 0, so a snapshot was written and replayed nothing. `anyConfigured` is
 *    `undefined` for the second, since without the list nothing can be said about what is
 *    configured;
 * 4. not computed - no snapshot, or one that replayed nothing while fills exist. Rows 2 and
 *    3 have ruled out "no trades", so this is fills whose snapshot predates them;
 * 5. otherwise the snapshot replayed trades and holds nothing: every one is between
 *    stablecoins, which are held at cost.
 *
 * A failure outranks an absence: "no trades" and "the sync failed" look identical on screen
 * and mean opposite things, so the failing case is the one that must never be shadowed.
 *
 * `exchanges === undefined` means the exchanges query failed, so row 2 cannot be decided and
 * the first half of row 3 is skipped rather than guessed. The caller handles "still loading"
 * itself.
 */
export function describeEmptyPositions(
  data: Positions,
  exchanges: readonly Exchange[] | undefined,
): EmptyPositions {
  if (data.last_recompute?.outcome === 'failed') {
    return {
      kind: 'recompute_failed',
      at: data.last_recompute.at,
      error: data.last_recompute.error,
    };
  }

  if (exchanges !== undefined) {
    const venues = venuesWithFailedSync(exchanges);
    if (venues.length > 0) {
      return { kind: 'sync_failed', venues };
    }
    // The snapshot outranks the list: a list polled before a stablecoin-only sync landed still
    // says 0 fills while the snapshot, written after it, has replayed events. Telling the owner
    // "no trades imported yet" then would contradict the data on the page.
    if (
      exchanges.every((exchange) => exchange.fills_stored === 0) &&
      (data.computed_at === null || data.event_count === 0)
    ) {
      return {
        kind: 'no_trades',
        anyConfigured: exchanges.some((exchange) => exchange.configured),
      };
    }
  } else if (data.computed_at !== null && data.event_count === 0) {
    return { kind: 'no_trades', anyConfigured: undefined };
  }

  return data.computed_at === null || data.event_count === 0
    ? { kind: 'not_computed' }
    : { kind: 'no_positions' };
}
