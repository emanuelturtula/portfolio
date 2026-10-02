/**
 * The invested-per-asset dashboard's pure logic: which positions are held, which words go
 * with each data-quality flag and exclusion, and which of the empty states applies. See
 * docs/specs/022-invested-per-asset-dashboard.md. The holdings check beside it - what the
 * replay says is held against the balances read - keeps its words and its partitions at the
 * end of this module, see docs/specs/025-holdings-reconciliation.md.
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
import type {
  Exclusion,
  LastRecompute,
  Position,
  Positions,
  Reconciliation,
  ReconciliationAsset,
  ReconciliationExchange,
} from '@/api/accounting';
import type { Exchange } from '@/api/exchanges';
import type { components } from '@/api/generated/schema';
import {
  errorSentence,
  EXCHANGES,
  hasFailedSync,
  type ExchangeKey,
  type ExchangeSyncErrorKind,
} from '@/lib/exchanges';
import { equalsMoney, formatMoney, isZeroMoney, money, type FormatMoneyOptions } from '@/lib/money';
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
 * What sales brought in for units with no known cost to compare them with. The summary's
 * term for the portfolio figure and the mark beside a closed asset in the line for those no
 * longer held: one constant, so the mark is the figure's own name and cannot drift from it.
 */
export const UNMATCHED_PROCEEDS_LABEL = 'Unmatched proceeds';

/**
 * Printed under the summary whenever the figure is shown - and so whenever the mark is on
 * screen, since both follow one condition (spec 026). It says the figure is net of fees, why
 * it is not in realized P&L, and that, like realized P&L, it covers every position.
 *
 * It names both origins of "no known cost": units that arrived without one, and units sold
 * beyond what the imported history held. On this page "Unknown cost" is a badge of its own
 * (`unknown_basis`), and units sold beyond the history are `history_incomplete` (spec 022,
 * R8, N2). A sentence naming only "units with no known cost" would contradict a row that
 * carries "History incomplete" and no "Unknown cost" (spec 026, R2).
 */
export const UNMATCHED_PROCEEDS_EXPLANATION =
  'Unmatched proceeds are what sales brought in, net of fees, for units with no known cost: ' +
  'units that arrived without one, or units sold beyond what the imported history held. ' +
  'They are kept out of realized P&L, because there is no cost to compare them with. Like ' +
  'realized P&L, the figure covers every position, held or not.';

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
 * Whether this position carries unmatched proceeds - what sales brought in for units with no
 * known cost to compare them with - held or not. Compared as a decimal, never as a string:
 * the wire spells a zero with as many places as the amounts around it. The figure is signed -
 * a fee paid in a third asset can make it negative - so "not zero" is the test, not
 * "positive".
 */
export function hasUnmatchedProceeds(position: Position): boolean {
  return !isZeroMoney(money(position.unmatched_proceeds));
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
 * each, with the label of every flag it carries in brackets beside it, then
 * {@link UNMATCHED_PROCEEDS_LABEL} when the asset carries those, and "Held exceeds history"
 * last when `heldExceedsHistory` has the asset: "2 assets no longer held are not listed: BTC
 * (History incomplete, Unmatched proceeds, Held exceeds history), ETH. Their realized P&L is
 * in the total."
 *
 * The flags are named here because `history_incomplete` and `unattributed_fee` are sticky:
 * `dispose` empties the pool when a disposal exceeds it, so the asset with the worst history
 * is often exactly the one that has no row.
 *
 * "Unmatched proceeds" is named here because it is the sharpest case of the figure: an asset
 * whose units all had unknown cost and were all sold. `unknown_basis` is not sticky, so that
 * position is closed and carries no flag, its realized P&L is `0`, and the line would say
 * nothing about money that did come in. It is the label and never an amount: this line is a
 * string, and an amount on this page is always a `<Money>` element. The amount is in the
 * summary's list, which is on screen whenever this mark is.
 *
 * "Held exceeds history" is named here for the sharpest form of what the holdings check finds:
 * the history says the asset is no longer held while the balances read say it is. A marker
 * that stayed off the page because the position has no row would hide exactly that. It is
 * text in the sentence, not a link: the badge on a row is the link.
 *
 * "No longer held", not "fully sold": a fee paid in an asset the history never held leaves a
 * position at zero without a sale ever having happened.
 */
export function describeClosedPositions(
  closed: readonly Position[],
  heldExceedsHistory: ReadonlySet<string>,
): string {
  const { noun, verb, possessive } = closed.length === 1 ? ONE_CLOSED : MANY_CLOSED;
  const labels = closed.map((position) => {
    const markers = position.flags.map((flag) => FLAG_BADGES[flag]);
    if (hasUnmatchedProceeds(position)) {
      markers.push(UNMATCHED_PROCEEDS_LABEL);
    }
    if (heldExceedsHistory.has(position.asset)) {
      markers.push(HELD_EXCEEDS_HISTORY_BADGE);
    }

    return markers.length === 0 ? position.asset : `${position.asset} (${markers.join(', ')})`;
  });

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

/** The id of the holdings check's heading: where the badge on a position takes the reader. */
export const HOLDINGS_CHECK_ID = 'holdings-check';

/**
 * What the block compares, in one sentence, and how far apart the two sides may be before it
 * says so: a difference of `tolerance_pct` percent **or less** is a match (the rule is `<=`).
 * The percentage is the response's, as sent - the page keeps no copy of the rule. It is a
 * constant of that rule and not a quantity of the owner's, so it is text, not a `<data>`
 * element.
 *
 * "The history", not "the imported trades": a manual adjustment is part of it.
 */
export function describeComparison(tolerancePct: string): string {
  return (
    'Compares what the history says is held with the balances read from your wallets ' +
    `and from your exchanges' spot accounts. A difference of ${formatMoney(money(tolerancePct))}% ` +
    'or less counts as a match.'
  );
}

/** The text on the badge beside a held asset whose balances exceed its history. */
export const HELD_EXCEEDS_HISTORY_BADGE = 'Held exceeds history';

/**
 * The legend's line for {@link HELD_EXCEEDS_HISTORY_BADGE}, printed with the flags'. It states
 * the finding and the usual cause as a cause, not as a fact: two readings taken at different
 * moments can show the same gap for coins that were only in transit.
 */
export const HELD_EXCEEDS_HISTORY_EXPLANATION =
  'The balances read hold more of this asset than the history accounts for. The usual cause ' +
  'is a buy the history does not show, which leaves those units out of its average cost and ' +
  'profit; coins in transit between two readings can look the same. The holdings check ' +
  'below says more.';

/**
 * The last recompute of the history, when it failed. Then the history is older than the
 * balances, and every asset bought since would look like balances above the history: the
 * block compares nothing, and no badge is drawn. `null` for a recompute that was written and
 * for none since the process started (`last_recompute` lives in memory).
 */
export function failedRecompute(lastRecompute: LastRecompute | null): LastRecompute | null {
  return lastRecompute?.outcome === 'failed' ? lastRecompute : null;
}

/**
 * The assets whose balances exceed their history - status `history_short`. Empty while the
 * reconciliation is loading or failed, and while the history may be older than the balances (a
 * failed recompute): no badge is drawn from a reading that is not there, or from a comparison
 * the block itself does not show.
 */
export function heldExceedsHistoryAssets(
  reconciliation: Reconciliation | undefined,
): ReadonlySet<string> {
  if (reconciliation === undefined || failedRecompute(reconciliation.last_recompute) !== null) {
    return new Set();
  }

  return new Set(
    reconciliation.assets
      .filter((entry) => entry.status === 'history_short')
      .map((entry) => entry.asset),
  );
}

/**
 * The two lists of the block, in the endpoint's order (sorted by asset). `match` is in
 * neither: a quantity that matches needs no line of its own, and the block says so once.
 */
export function partitionReconciliation(assets: readonly ReconciliationAsset[]): {
  readonly short: readonly ReconciliationAsset[];
  readonly over: readonly ReconciliationAsset[];
} {
  return {
    short: assets.filter((entry) => entry.status === 'history_short'),
    over: assets.filter((entry) => entry.status === 'history_over'),
  };
}

/**
 * What to say to the owner of a `history_short` asset: the finding, the usual cause and what
 * it does to the figures, the thing to rule out first, and the way out. It is a prompt to
 * look and not a verdict: the readings are taken at different moments, so coins moved between
 * two of them are counted twice until both have been read again - not "until the next sync",
 * since a source that has stopped being read stays compared for up to the reading age limit -
 * and the block shows below the lists how old each reading is. The way out is the line
 * beneath it, {@link RECORD_MISSING_COINS_PROMPT}, which links to the page that records an
 * adjustment (spec 027).
 */
export const HELD_EXCEEDS_HISTORY_GUIDANCE =
  'The balances read hold more than the history accounts for. The usual cause is buys older ' +
  'than an exchange keeps, or coins acquired elsewhere, and average cost and profit then ' +
  'leave those units out. Before recording anything, rule out coins in transit: readings are ' +
  'taken at different moments, so coins moved between two of them are counted twice until ' +
  'both have been read again. How old each reading is, is shown below the lists.';

/**
 * The line after the guidance, followed by one link per `history_short` asset to the page that
 * records the missing coins, with that asset carried over. It says "the missing coins" and not
 * "an opening balance": the gap can be coins held before the history begins or coins acquired
 * elsewhere since, and which of the two it is decides the date the adjustment should carry
 * (spec 027, R8). The quantity is never carried over: the difference shown can include coins in
 * transit between two readings (spec 025, R9 and R10).
 */
export const RECORD_MISSING_COINS_PROMPT = 'If the gap is real, record the missing coins for:';

/**
 * What to say about a `history_over` asset. Never worded as an error or as a thing to fix:
 * the balances read are a lower bound on what the owner holds, and several unrelated causes
 * put the history above them - including a sale or conversion the import did not see - which
 * this check cannot tell apart. So it says what they are and flags nothing.
 */
export const HISTORY_EXCEEDS_HELD_EXPLANATION =
  'The history accounts for more than the balances read. The causes include coins held where ' +
  'this application does not read them (another wallet, an Earn product, or a futures or ' +
  'funding account), withdrawals, network fees and trading fees the import did not record, ' +
  'and a sale or conversion the import did not see. This check cannot tell them apart, so it ' +
  'flags nothing.';

/** Said under each table: the sign of the last column is read off this. */
export const DIFFERENCE_LEGEND =
  'Difference is the wallets plus the exchanges, minus what the history accounts for.';

export const ALL_QUANTITIES_MATCH = 'Every quantity matches the balances read.';
export const NOTHING_TO_COMPARE = 'There is nothing to compare yet.';

/**
 * The kinds that mean the venue refused the key. For a balance read the exchanges page's
 * sentences are wrong twice over: "does not have read permission" is untrue of a key that has
 * just read the trades, and a refused key is not asked again by a scheduled sync, which the
 * owner needs to know to understand why nothing changes by itself.
 */
const KEY_REFUSED_KINDS: readonly ExchangeSyncErrorKind[] = ['auth', 'insufficient_scope'];

/**
 * The sentence for a failed balance read. `errorSentence` is the exchanges page's own
 * vocabulary, reused so the two pages cannot drift, with two exceptions:
 *
 * - `auth` and `insufficient_scope` say the key was refused for the balance read, that
 *   scheduled syncs will not ask again, and that a sync from the Exchanges page retries once
 *   the key is fixed - the read is skipped by every trigger but a manual one;
 * - `internal` says the sync was stopped, which for a balance read is untrue: the fills are in
 *   and the sync succeeded; only the read after it failed.
 *
 * Every other kind uses `errorSentence` as it is, and no branch depends on which of them can
 * occur. Two are worded for fills - `retention_window` ("a window of history", which Bitget's
 * error map can still produce for any request) and `conflict` ("a fill ... differs") - and
 * read oddly for a balance; they are in the type because the column's `CHECK` mirrors the whole
 * of `ExchangeSyncErrorKind`.
 */
export function balancesErrorSentence(kind: ExchangeSyncErrorKind, venue: string): string {
  if (KEY_REFUSED_KINDS.includes(kind)) {
    return (
      `${venue} refused the API key for the balance read. Scheduled syncs will not ask ` +
      'again; a sync from the Exchanges page retries once the key is fixed.'
    );
  }

  return kind === 'internal'
    ? 'A defect in this application stopped the balances from being read. The container log has the details.'
    : errorSentence(kind, venue);
}

/**
 * A source left out of the comparison, and why. The venue kinds are the response's
 * `not_compared_reason`; the wallets are counts. Left out is **nothing contributed** - never a
 * stale figure kept in the sum, which could count coins twice (spec 025, R9) - so each says
 * which source it is, since a source that is missing can hide a real `history_short`.
 */
export type MissingSource =
  | {
      readonly kind: 'read_failed';
      readonly venue: ExchangeKey;
      readonly error: ExchangeSyncErrorKind;
      /** The last good reading, which is not used; `null` when none was ever taken. */
      readonly lastReadAt: string | null;
    }
  | { readonly kind: 'never_read'; readonly venue: ExchangeKey }
  | { readonly kind: 'sync_failed'; readonly venue: ExchangeKey; readonly lastReadAt: string }
  | {
      readonly kind: 'out_of_date';
      readonly venue: ExchangeKey;
      readonly lastReadAt: string;
      readonly maxAgeHours: number;
    }
  | { readonly kind: 'wallets_stale'; readonly count: number; readonly maxAgeHours: number }
  | { readonly kind: 'wallets_unread'; readonly count: number };

/**
 * `read_failed` is the first reason that applies, and it applies exactly when `balances_error`
 * is set: the contract the backend writes (spec 025, "Design: service and endpoint"). Narrowing
 * here, once, is what lets the notice take an error kind instead of carrying a fallback for a
 * `read_failed` that has none.
 */
function isReadFailed(
  exchange: ReconciliationExchange,
): exchange is ReconciliationExchange & { balances_error: ExchangeSyncErrorKind } {
  return exchange.not_compared_reason === 'read_failed';
}

/**
 * `sync_failed` and `out_of_date` come after `never_read` in the backend's order, so a venue
 * with either has been read at least once and `balances_read_at` is set. Narrowed once, here,
 * for the same reason as {@link isReadFailed}.
 */
function hasReadingLeftOut(
  exchange: ReconciliationExchange,
): exchange is ReconciliationExchange & { balances_read_at: string } {
  return (
    exchange.not_compared_reason === 'sync_failed' || exchange.not_compared_reason === 'out_of_date'
  );
}

/** The venue's notice, when its reading was not compared, in the backend's own order of reasons. */
function venueNotice(exchange: ReconciliationExchange, maxAgeHours: number): MissingSource[] {
  const venue = exchange.exchange_key;

  if (isReadFailed(exchange)) {
    return [
      {
        kind: 'read_failed',
        venue,
        error: exchange.balances_error,
        lastReadAt: exchange.balances_read_at,
      },
    ];
  }
  if (exchange.not_compared_reason === 'never_read') {
    return [{ kind: 'never_read', venue }];
  }
  if (hasReadingLeftOut(exchange)) {
    return [
      exchange.not_compared_reason === 'sync_failed'
        ? { kind: 'sync_failed', venue, lastReadAt: exchange.balances_read_at }
        : { kind: 'out_of_date', venue, lastReadAt: exchange.balances_read_at, maxAgeHours },
    ];
  }

  return [];
}

/**
 * The sources left out of the comparison: the venues first, in the endpoint's order, then the
 * wallets that are stale, then the wallets that were never read. A venue whose reading was
 * compared has no entry.
 */
export function missingSources(
  data: Pick<Reconciliation, 'exchanges' | 'wallets' | 'max_reading_age_hours'>,
): MissingSource[] {
  const maxAgeHours = data.max_reading_age_hours;
  const venues = data.exchanges.flatMap((exchange) => venueNotice(exchange, maxAgeHours));
  const { stale, unread } = data.wallets;

  return [
    ...venues,
    ...(stale > 0 ? [{ kind: 'wallets_stale' as const, count: stale, maxAgeHours }] : []),
    ...(unread > 0 ? [{ kind: 'wallets_unread' as const, count: unread }] : []),
  ];
}

/** "The balances at Bitget could not be read. Bitget refused the API key for the balance read. ..." */
export function describeBalancesFailure(venue: ExchangeKey, error: ExchangeSyncErrorKind): string {
  const name = EXCHANGES[venue].name;

  return `The balances at ${name} could not be read. ${balancesErrorSentence(error, name)}`;
}

/** What a failed read leaves the comparison with when no reading was ever taken. */
export function describeNeverRead(venue: ExchangeKey): string {
  return `No balances have been read from ${EXCHANGES[venue].name}, so the coins held there are left out of the comparison.`;
}

/** A venue with no error and no reading: its first read has not come round yet. */
export function describeNotReadYet(venue: ExchangeKey): string {
  const name = EXCHANGES[venue].name;

  return (
    `The balances at ${name} have not been read yet. They are read after its next ` +
    'successful sync, and until then the coins held there are left out of the comparison.'
  );
}

/** A venue whose last sync failed: the balances behind it are not trusted, whatever age. */
export function describeSyncFailed(venue: ExchangeKey): string {
  return (
    `The last sync of ${EXCHANGES[venue].name} failed, so its balances were not read and ` +
    'are left out of the comparison.'
  );
}

/** Why a venue whose last reading is old is left out: the limit is the rule, not a judgement. */
export function describeOutOfDate(maxAgeHours: number): string {
  return `A reading older than ${String(maxAgeHours)} hours is left out of the comparison.`;
}

/** "1 wallet was last read more than 24 hours ago, so the coins in it are left out ..." */
export function describeStaleWallets(count: number, maxAgeHours: number): string {
  const age = `more than ${String(maxAgeHours)} hours ago`;

  return count === 1
    ? `1 wallet was last read ${age}, so the coins in it are left out of the comparison.`
    : `${String(count)} wallets were last read ${age}, so the coins in them are left out of the comparison.`;
}

/** "3 wallets have not been read yet, so the coins in them are left out of the comparison." */
export function describeUnreadWallets(count: number): string {
  return count === 1
    ? '1 wallet has not been read yet, so the coins in it are left out of the comparison.'
    : `${String(count)} wallets have not been read yet, so the coins in them are left out of the comparison.`;
}

/** When a source was last read: its name and the instant, in the order they are listed. */
export interface BalanceReading {
  readonly label: string;
  readonly at: string;
}

/**
 * A venue whose reading was compared: `not_compared_reason` is `null`, and a venue with a
 * reading to compare has one (`balances_read_at`). Narrowed once, here, like the guards above.
 */
function isCompared(
  exchange: ReconciliationExchange,
): exchange is ReconciliationExchange & { balances_read_at: string } {
  return exchange.not_compared_reason === null;
}

/**
 * When each source that **was compared** was last read: every venue whose reading is current,
 * then the wallets' **oldest** compared observation. A source left out is in
 * {@link missingSources} and not here: a time beside a figure that is not in the sum would
 * read as if it were. The comparison is as old as its oldest input, so the wallets show their
 * oldest and not their newest.
 */
export function balanceReadings(
  data: Pick<Reconciliation, 'exchanges' | 'wallets'>,
): BalanceReading[] {
  const venues = data.exchanges.filter(isCompared).map((exchange) => ({
    label: EXCHANGES[exchange.exchange_key].name,
    at: exchange.balances_read_at,
  }));
  const oldest = data.wallets.oldest_observed_at;

  return oldest === null ? venues : [...venues, { label: 'Wallets (oldest reading)', at: oldest }];
}
