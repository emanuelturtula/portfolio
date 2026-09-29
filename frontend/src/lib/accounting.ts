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
import { EXCHANGES, type ExchangeKey } from '@/lib/exchanges';
import { isZeroMoney, money, type FormatMoneyOptions } from '@/lib/money';
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
 * every flag that appears in it.
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
    'Part of the holding has no known cost: it was deposited, or bought before the imported ' +
    'history begins. Its average cost, invested and unrealized P&L cover only the part with ' +
    'a known cost, and it is left out of the portfolio totals.',
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
  unpriced: 'no price is available, so there is a cost and no value.',
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

const ONE_CLOSED = { noun: 'asset', verb: 'is', possessive: 'its' };
const MANY_CLOSED = { noun: 'assets', verb: 'are', possessive: 'their' };

/**
 * The one line that stands for every asset no longer held that the table leaves out, e.g.
 * "2 assets no longer held (BTC, ETH) are not listed; their realized P&L is in the total."
 *
 * "No longer held", not "fully sold": a fee paid in an asset the history never held leaves a
 * position at zero without a sale ever having happened.
 */
export function describeClosedPositions(symbols: readonly string[]): string {
  const { noun, verb, possessive } = symbols.length === 1 ? ONE_CLOSED : MANY_CLOSED;

  return (
    `${String(symbols.length)} ${noun} no longer held (${symbols.join(', ')}) ` +
    `${verb} not listed; ${possessive} realized P&L is in the total.`
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
 * A venue that has never synced, or is syncing, has not failed.
 */
export function venuesWithFailedSync(exchanges: readonly Exchange[]): ExchangeKey[] {
  return exchanges
    .filter((exchange) => exchange.status === 'error' || exchange.status === 'auth_failed')
    .map((exchange) => exchange.exchange_key);
}

/**
 * Which of the five empty states applies, when `positions` is empty. A discriminated union
 * rather than a string, so each component can only read the fields its own state carries.
 */
export type EmptyPositions =
  | { readonly kind: 'recompute_failed'; readonly at: string; readonly error: string | null }
  | { readonly kind: 'sync_failed'; readonly venues: readonly ExchangeKey[] }
  | { readonly kind: 'no_trades'; readonly anyConfigured: boolean }
  | { readonly kind: 'not_computed' }
  | { readonly kind: 'no_positions' };

/**
 * Tells an empty response apart from the four ways it can happen, first match wins:
 *
 * 1. the last recompute failed - ours, and the snapshot served is the one before it;
 * 2. a venue's last sync failed - trades may be missing, which is the opposite of "none";
 * 3. no venue has stored a fill - the owner has not imported anything yet;
 * 4. no snapshot has been written yet;
 * 5. otherwise every trade so far is between stablecoins, which are held at cost.
 *
 * A failure outranks an absence: "no trades" and "the sync failed" look identical on screen
 * and mean opposite things, so the failing case is the one that must never be shadowed.
 *
 * `exchanges === undefined` means the exchanges query failed, so rows 2 and 3 cannot be
 * decided and are skipped rather than guessed. The caller handles "still loading" itself.
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
    if (exchanges.every((exchange) => exchange.fills_stored === 0)) {
      return {
        kind: 'no_trades',
        anyConfigured: exchanges.some((exchange) => exchange.configured),
      };
    }
  }

  return data.computed_at === null ? { kind: 'not_computed' } : { kind: 'no_positions' };
}
