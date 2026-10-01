import Decimal from 'decimal.js';

import type { components } from '@/api/generated/schema';

import {
  COMPUTED_AT,
  lastRecompute,
  ZERO,
  type LastRecomputeResponse,
  type PositionsResponse,
} from './accountingFixtures';
import type { ExchangeKey, ExchangeSyncErrorKind } from './exchangeFixtures';
import { NOW } from './fixtures';

/**
 * Fixture builders for `GET /api/accounting/reconciliation` (spec 025).
 *
 * Typed with the generated OpenAPI types and, like `accountingFixtures.ts`, **checked against
 * what the backend can write**. Every figure of an asset is derived from three others - what
 * is held is the wallets plus the exchanges, the difference is what is held minus the
 * history, and the status is the difference against the tolerance - so a fixture stating a
 * `history_short` whose numbers are a match would test the page against a response
 * `domain/accounting/reconciliation.py` never produces. Every builder below runs
 * {@link assertWritableReconciliation}, which re-derives all three and refuses a response
 * that disagrees.
 *
 * The figures in the scenarios are still written out by hand, in the comment beside each.
 * `decimal.js` is used here only to **check** them, on a private clone, never to produce an
 * expectation a test asserts - and never through `@/lib/money`, the code under test.
 *
 * Every quantity is an 18-place string, the scale the comparison carries (`AMOUNT_SCALE`).
 * No figure here is a JavaScript number, and none is anybody's real balance.
 *
 * **A reading is compared only while it is current** (spec 025, R9), and whether it is
 * depends on the clock: the guard measures every reading against `NOW`, the instant every
 * page test runs under, the way `services/reconciliation.py` measures it against its own.
 */

type Schemas = components['schemas'];

export type ReconciliationResponse = Schemas['ReconciliationResponse'];
export type AssetReconciliationResponse = Schemas['AssetReconciliationResponse'];
export type ExchangeBalancesResponse = Schemas['ExchangeBalancesResponse'];
export type WalletsReadResponse = Schemas['WalletsReadResponse'];
export type ReconciliationStatus = Schemas['ReconciliationStatus'];
export type NotComparedReason = Schemas['NotComparedReason'];

/** Every status, as a `Record` whose values are ignored: a new one fails `tsc` here. */
const STATUSES_RECORD: Record<ReconciliationStatus, true> = {
  history_over: true,
  history_short: true,
  match: true,
};
export const ALL_RECONCILIATION_STATUSES = Object.keys(STATUSES_RECORD) as ReconciliationStatus[];

/** Every reason, in the order the backend tests them: the first that applies is the answer. */
const REASONS_RECORD: Record<NotComparedReason, true> = {
  read_failed: true,
  never_read: true,
  sync_failed: true,
  out_of_date: true,
};
export const ALL_NOT_COMPARED_REASONS = Object.keys(REASONS_RECORD) as NotComparedReason[];

/*
 * Instants. Every page test runs under `NOW` (2026-09-24T12:00:00Z). The ones a relative time
 * is read from sit on whole minutes. A reading's instant carries microseconds, as the backend
 * serialises a clock reading; the snapshot's `computed_at` is `accountingFixtures`'.
 */

/** When a venue's balances were last read: 14 minutes before `NOW`, after its sync. */
export const BALANCES_READ_AT = '2026-09-24T11:46:00.000000Z';
/** A second venue's reading: 16 minutes before `NOW`. */
export const OTHER_BALANCES_READ_AT = '2026-09-24T11:44:00.000000Z';
/**
 * A reading that is no longer refreshed: 3 days before `NOW`, three times the age limit. What
 * a venue keeps after its read or its sync started failing, and what makes one out of date.
 */
export const OLD_BALANCES_READ_AT = '2026-09-21T12:00:00.000000Z';
/** A reading exactly at the age limit, 24 hours before `NOW`: at most that old, so current. */
export const EDGE_BALANCES_READ_AT = '2026-09-23T12:00:00.000000Z';
/** One second past the age limit: out of date. */
export const PAST_EDGE_BALANCES_READ_AT = '2026-09-23T11:59:59.000000Z';
/** The oldest wallet reading that is compared: 20 minutes before `NOW`. */
export const WALLETS_OBSERVED_AT = '2026-09-24T11:40:00.000000Z';

/** `MAX_READING_AGE_HOURS`: how old a reading may be and still be compared. */
export const MAX_READING_AGE_HOURS = 24;

/** `RECONCILIATION_TOLERANCE_PCT`, as `str(Decimal(1))`. */
export const TOLERANCE_PCT = '1';

const AMOUNT_SCALE = 18;

/** `DEFAULT_CASH_ASSETS`: the engine keeps no quantity for them, so they are never compared. */
const CASH_ASSETS: readonly string[] = ['USDC', 'USDT'];

/** `ChainKey.asset_symbol`'s values: the only assets a wallet can hold. */
const WALLET_ASSETS: readonly string[] = ['BTC', 'KAS'];

function fail(message: string): never {
  throw new Error(`Impossible reconciliation fixture: ${message}`);
}

/** A private constructor for the checks: nothing here depends on `lib/money.ts`'s precision. */
const Exact = Decimal.clone({ precision: 60, rounding: Decimal.ROUND_HALF_EVEN });
type Exact = Decimal;

const PLAIN_DECIMAL = /^-?\d+(?:\.(\d+))?$/;

/** Parses a wire quantity, refusing one at the wrong scale: the scale is part of the shape. */
function exact(label: string, value: string): Exact {
  const match = PLAIN_DECIMAL.exec(value);
  if (match === null) {
    fail(`${label} "${value}" is not a plain decimal string.`);
  }
  const places = match[1]?.length ?? 0;
  if (places !== AMOUNT_SCALE) {
    fail(
      `${label} "${value}" has ${String(places)} places; the backend sends ${String(AMOUNT_SCALE)}.`,
    );
  }
  return new Exact(value);
}

/**
 * `reconcile`'s rule, exactly: a match when `|difference| x 100 <= tolerance x max(history,
 * held)`, with no division and no rounding; otherwise short when more is held than the
 * history accounts for, and over when less is.
 */
function statusOf(history: Exact, held: Exact, tolerance: Exact): ReconciliationStatus {
  const difference = held.minus(history);
  if (
    difference
      .abs()
      .times(100)
      .lte(tolerance.times(Exact.max(history, held)))
  ) {
    return 'match';
  }
  return difference.isPositive() ? 'history_short' : 'history_over';
}

function isCount(value: number): boolean {
  return Number.isInteger(value) && value >= 0;
}

/** How long before `NOW` an instant is, in milliseconds. Negative for one after it. */
function ageOf(instant: string): number {
  // `Date` is specified for three fractional digits; the backend sends six.
  return Date.parse(NOW) - Date.parse(instant.replace(/(\.\d{3})\d+/, '$1'));
}

/**
 * Throws unless `entry` is a venue `not_compared_reason` can describe. The reasons are tested
 * in order and the first that applies is the answer, so each one pins the fields before it:
 *
 * - `read_failed` exactly when `balances_error` is set, reading or not;
 * - `never_read` exactly when there is no error and no reading;
 * - `sync_failed`, `out_of_date` and `null` all have a reading and no error. Which of them it
 *   is turns first on the account's fill-sync status, which this response does not carry, and
 *   then on the reading's age: a compared reading is at most the limit old, and an
 *   `out_of_date` one is older.
 */
function assertWritableVenue(entry: ExchangeBalancesResponse, limitMs: number): void {
  const key = entry.exchange_key;
  const reason = entry.not_compared_reason;

  if (entry.balances_error !== null) {
    if (reason !== 'read_failed') {
      fail(`${key}: a failed balance read is read_failed, whatever was read before it.`);
    }
    return;
  }
  if (entry.balances_read_at === null) {
    if (reason !== 'never_read') {
      fail(`${key}: no error and no reading is never_read.`);
    }
    return;
  }
  if (reason === 'read_failed' || reason === 'never_read') {
    fail(`${key}: ${reason} is for a venue with an error, or with no reading at all.`);
  }
  const tooOld = ageOf(entry.balances_read_at) > limitMs;
  if (reason === 'out_of_date' && !tooOld) {
    fail(`${key}: a reading at most the age limit old is current, not out_of_date.`);
  }
  if (reason === null && tooOld) {
    fail(`${key}: a reading older than the age limit is not compared: it is out_of_date.`);
  }
}

/** Throws unless `response` is one `GET /api/accounting/reconciliation` can serve. */
export function assertWritableReconciliation(
  response: ReconciliationResponse,
): ReconciliationResponse {
  if (!/^\d+(?:\.\d+)?$/.test(response.tolerance_pct)) {
    fail(`tolerance_pct "${response.tolerance_pct}" is not a plain non-negative decimal.`);
  }
  const tolerance = new Exact(response.tolerance_pct);

  if (response.computed_at === null && response.assets.length > 0) {
    fail('with no snapshot there is nothing to compare: computed_at is null and assets is empty.');
  }

  const names = response.assets.map((entry) => entry.asset);
  if (names.some((name, index) => index > 0 && (names[index - 1] ?? '') >= name)) {
    fail('assets are one per asset, sorted by asset.');
  }

  if (!Number.isInteger(response.max_reading_age_hours) || response.max_reading_age_hours < 1) {
    fail('max_reading_age_hours is a whole number of hours.');
  }
  const limitMs = response.max_reading_age_hours * 3_600_000;

  const recompute = response.last_recompute;
  if (recompute !== null && (recompute.outcome === 'failed') !== (recompute.error !== null)) {
    fail('a failed recompute records its error class, and no other outcome has one.');
  }
  if (response.computed_at === null && recompute !== null && recompute.outcome !== 'failed') {
    fail(`a recompute that was ${recompute.outcome} left a snapshot behind it.`);
  }

  response.exchanges.forEach((entry) => {
    assertWritableVenue(entry, limitMs);
  });
  const anyVenueCompared = response.exchanges.some((entry) => entry.not_compared_reason === null);

  for (const entry of response.assets) {
    const asset = entry.asset;
    if (CASH_ASSETS.includes(asset)) {
      fail(`${asset} is a cash asset: the engine keeps no quantity for it, so it is not compared.`);
    }
    const history = exact(`${asset} history_quantity`, entry.history_quantity);
    const wallets = exact(`${asset} wallet_quantity`, entry.wallet_quantity);
    const exchanges = exact(`${asset} exchange_quantity`, entry.exchange_quantity);
    const held = exact(`${asset} held_quantity`, entry.held_quantity);
    const difference = exact(`${asset} difference`, entry.difference);

    if (history.isNeg() || wallets.isNeg() || exchanges.isNeg()) {
      fail(`${asset}: a quantity in the history, in a wallet or on an exchange is never negative.`);
    }
    if (!held.eq(wallets.plus(exchanges))) {
      fail(
        `${asset} held_quantity is ${entry.held_quantity}; the wallets and the exchanges add ` +
          `up to ${wallets.plus(exchanges).toFixed(AMOUNT_SCALE)}.`,
      );
    }
    if (!difference.eq(held.minus(history))) {
      fail(
        `${asset} difference is ${entry.difference}; held minus history is ` +
          `${held.minus(history).toFixed(AMOUNT_SCALE)}.`,
      );
    }
    if (history.isZero() && held.isZero()) {
      fail(`${asset}: an asset with nothing in the history and nothing held is left out.`);
    }
    const expected = statusOf(history, held, tolerance);
    if (entry.status !== expected) {
      fail(
        `${asset} is stated ${entry.status}; a difference of ${entry.difference} against a ` +
          `tolerance of ${response.tolerance_pct} percent is ${expected}.`,
      );
    }
    if (!wallets.isZero() && !WALLET_ASSETS.includes(asset)) {
      fail(`${asset}: only a chain's own asset (${WALLET_ASSETS.join(', ')}) is held in a wallet.`);
    }
    if (!wallets.isZero() && response.wallets.compared === 0) {
      fail(`${asset}: a wallet quantity comes from a wallet that is compared, and none is.`);
    }
    if (!exchanges.isZero() && !anyVenueCompared) {
      fail(
        `${asset}: an exchange quantity comes from a venue that is compared, and none is: a ` +
          'reading that is not current contributes nothing.',
      );
    }
  }

  const keys = response.exchanges.map((entry) => entry.exchange_key);
  if (keys.some((key, index) => index > 0 && (keys[index - 1] ?? '') >= key)) {
    fail('exchanges lists one entry per account, by exchange_key.');
  }

  const { compared, stale, unread, oldest_observed_at: oldest } = response.wallets;
  if (!isCount(compared) || !isCount(stale) || !isCount(unread)) {
    fail('wallets.compared, stale and unread are counts, so non-negative integers.');
  }
  if ((compared === 0) !== (oldest === null)) {
    fail('wallets.oldest_observed_at is the oldest reading among the compared wallets, or null.');
  }
  if (oldest !== null && ageOf(oldest) > limitMs) {
    fail('wallets.oldest_observed_at is of a compared wallet, so at most the age limit old.');
  }

  return response;
}

/*
 * Builders.
 */

/**
 * BTC, held beyond what the history accounts for - the response spec 025 prints. Hand-worked:
 *
 * - the history holds 0.5;
 * - the wallets hold 0.7 and the exchanges 0.3: held 0.7 + 0.3 = 1.0;
 * - difference 1.0 - 0.5 = +0.5;
 * - 0.5 x 100 = 50 against 1 x max(0.5, 1.0) = 1: not a match, and positive, so `history_short`.
 *
 * Not checked on its own: a test that overrides one figure must override the ones derived
 * from it, and {@link reconciliation} checks the result.
 */
export function assetReconciliation(
  overrides: Partial<AssetReconciliationResponse> = {},
): AssetReconciliationResponse {
  return {
    asset: 'BTC',
    history_quantity: '0.500000000000000000',
    wallet_quantity: '0.700000000000000000',
    exchange_quantity: '0.300000000000000000',
    held_quantity: '1.000000000000000000',
    difference: '0.500000000000000000',
    status: 'history_short',
    ...overrides,
  };
}

/**
 * An asset whose history and balances agree to the last place: everything the history holds
 * sits on an exchange, and nothing in a wallet. Copies the quantity; no arithmetic.
 */
export function matchedAsset(asset: string, quantity: string): AssetReconciliationResponse {
  return {
    asset,
    history_quantity: quantity,
    wallet_quantity: ZERO,
    exchange_quantity: quantity,
    held_quantity: quantity,
    difference: ZERO,
    status: 'match',
  };
}

/**
 * ETH, held beyond the history, with every one of 18 places in use. Hand-worked:
 *
 * - the history holds 1.000000000000000001;
 * - the exchanges hold 3.141592653589793238, no wallet does: held 3.141592653589793238;
 * - difference 3.141592653589793238 - 1.000000000000000001 = +2.141592653589793237.
 *
 * As doubles these are 1, 3.141592653589793 and 2.141592653589793: a page that parsed any of
 * them would show it in a `<data value>`.
 */
export function ethShortPrecise(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'ETH',
    history_quantity: '1.000000000000000001',
    wallet_quantity: ZERO,
    exchange_quantity: '3.141592653589793238',
    held_quantity: '3.141592653589793238',
    difference: '2.141592653589793237',
  });
}

/**
 * KAS, held in a wallet and never traded: there is no history at all, so no position and no
 * row in the positions table. Held 250000, difference +250000.
 */
export function kasNeverTraded(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'KAS',
    history_quantity: ZERO,
    wallet_quantity: '250000.000000000000000000',
    exchange_quantity: ZERO,
    held_quantity: '250000.000000000000000000',
    difference: '250000.000000000000000000',
  });
}

/**
 * SOL, where the history holds more than the balances read. Hand-worked:
 *
 * - the history holds 10;
 * - the exchanges hold 7.5, no wallet does: held 7.5;
 * - difference 7.5 - 10 = -2.5;
 * - 2.5 x 100 = 250 against 1 x max(10, 7.5) = 10: not a match, and negative, so `history_over`.
 */
export function solOver(
  overrides: Partial<AssetReconciliationResponse> = {},
): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'SOL',
    history_quantity: '10.000000000000000000',
    wallet_quantity: ZERO,
    exchange_quantity: '7.500000000000000000',
    held_quantity: '7.500000000000000000',
    difference: '-2.500000000000000000',
    status: 'history_over',
    ...overrides,
  });
}

export const SOL_OVER = {
  history: '10.000000000000000000',
  wallets: ZERO,
  exchanges: '7.500000000000000000',
  held: '7.500000000000000000',
  difference: '-2.500000000000000000',
} as const;

/**
 * XRP, in the history and held nowhere this application reads: history 40, held 0, difference
 * -40. The whole position sits where nothing is read - or was withdrawn.
 */
export function xrpHeldElsewhere(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'XRP',
    history_quantity: '40.000000000000000000',
    wallet_quantity: ZERO,
    exchange_quantity: ZERO,
    held_quantity: ZERO,
    difference: '-40.000000000000000000',
    status: 'history_over',
  });
}

/**
 * DOGE, a hair under the balances and inside the tolerance. Hand-worked:
 *
 * - the history holds 1000 and the exchanges 995: difference -5;
 * - 5 x 100 = 500 against 1 x max(1000, 995) = 1000: a match, with a difference that is not 0.
 *
 * The fee a zero-fee historical import did not record, which is what the tolerance is for.
 */
export function dogeWithinTolerance(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'DOGE',
    history_quantity: '1000.000000000000000000',
    wallet_quantity: ZERO,
    exchange_quantity: '995.000000000000000000',
    held_quantity: '995.000000000000000000',
    difference: '-5.000000000000000000',
    status: 'match',
  });
}

/** A venue whose balances were read, 14 minutes ago, with nothing wrong: it is compared. */
export function exchangeBalances(
  overrides: Partial<ExchangeBalancesResponse> = {},
): ExchangeBalancesResponse {
  return {
    exchange_key: 'bitget',
    balances_read_at: BALANCES_READ_AT,
    balances_error: null,
    not_compared_reason: null,
    ...overrides,
  };
}

/**
 * `read_failed`: a venue whose last balance read failed. `previous` is the instant of the last
 * good reading it keeps - a failed read leaves the rows and `balances_read_at` of the one
 * before - or `null` for a venue whose balances have never been read. Either way it is left
 * out: nothing says the coins of the reading before the failure are still there.
 */
export function failedBalances(
  exchangeKey: ExchangeKey,
  kind: ExchangeSyncErrorKind,
  previous: string | null = OLD_BALANCES_READ_AT,
): ExchangeBalancesResponse {
  return {
    exchange_key: exchangeKey,
    balances_read_at: previous,
    balances_error: kind,
    not_compared_reason: 'read_failed',
  };
}

/**
 * `never_read`: an account row no balance read has succeeded for, and none has failed: its
 * fill sync has not succeeded since the read was introduced.
 */
export function unreadBalances(exchangeKey: ExchangeKey): ExchangeBalancesResponse {
  return {
    exchange_key: exchangeKey,
    balances_read_at: null,
    balances_error: null,
    not_compared_reason: 'never_read',
  };
}

/**
 * `sync_failed`: the account's fill sync is not `ok`, so nothing refreshes its reading. The
 * read itself never failed - no read is attempted after a failed sync - and the reading it
 * keeps is from `readAt`, however recent: a sync that failed five minutes ago leaves a reading
 * younger than the age limit and still not compared.
 */
export function syncFailedBalances(
  exchangeKey: ExchangeKey,
  readAt: string = OLD_BALANCES_READ_AT,
): ExchangeBalancesResponse {
  return {
    exchange_key: exchangeKey,
    balances_read_at: readAt,
    balances_error: null,
    not_compared_reason: 'sync_failed',
  };
}

/**
 * `out_of_date`: nothing failed, and the reading is older than the age limit - the timer is
 * off, or the credentials were removed after the read.
 */
export function outOfDateBalances(
  exchangeKey: ExchangeKey,
  readAt: string = OLD_BALANCES_READ_AT,
): ExchangeBalancesResponse {
  return {
    exchange_key: exchangeKey,
    balances_read_at: readAt,
    balances_error: null,
    not_compared_reason: 'out_of_date',
  };
}

/** No wallet registered: nothing compared, nothing left out. */
export const NO_WALLETS: WalletsReadResponse = {
  compared: 0,
  stale: 0,
  unread: 0,
  oldest_observed_at: null,
};

/**
 * `compared` wallets, the oldest of them read 20 minutes ago; `stale` ones whose reading is
 * older than the age limit and `unread` ones no sync has read, neither of which adds anything.
 */
export function walletReadings(
  compared: number,
  leftOut: { readonly stale?: number; readonly unread?: number } = {},
): WalletsReadResponse {
  return {
    compared,
    stale: leftOut.stale ?? 0,
    unread: leftOut.unread ?? 0,
    oldest_observed_at: compared === 0 ? null : WALLETS_OBSERVED_AT,
  };
}

/**
 * A response, checked. The default is a snapshot, written by a recompute that succeeded,
 * compared with one venue read 14 minutes ago and no wallet: nothing to compare, and nothing
 * left out.
 */
export function reconciliation(
  overrides: Partial<ReconciliationResponse> = {},
): ReconciliationResponse {
  return assertWritableReconciliation({
    computed_at: COMPUTED_AT,
    tolerance_pct: TOLERANCE_PCT,
    assets: [],
    max_reading_age_hours: MAX_READING_AGE_HOURS,
    last_recompute: lastRecompute(),
    exchanges: [exchangeBalances()],
    wallets: NO_WALLETS,
    ...overrides,
  });
}

/*
 * Scenarios.
 */

/**
 * No snapshot: `computed_at` is null and nothing is compared - "not computed", never "every
 * balance is unaccounted for". The sources are still answered. No recompute has been
 * attempted unless stated: the only one that leaves no snapshot behind is one that failed.
 */
export function notReconciled(
  overrides: Partial<Pick<ReconciliationResponse, 'exchanges' | 'wallets' | 'last_recompute'>> = {},
): ReconciliationResponse {
  return reconciliation({ computed_at: null, assets: [], last_recompute: null, ...overrides });
}

/**
 * The quiet answer that goes with `positions`: **every held position matches**, to the last
 * place, every unit of it sitting on Bitget, read 14 minutes ago and compared; no wallet, no
 * error, nothing left out. With no snapshot it is {@link notReconciled}, and with nothing held
 * it compares nothing and names no venue - the first-time owner's answer.
 *
 * `last_recompute` is the positions' own, because the two endpoints serve one status: over a
 * snapshot whose last recompute failed, the answer is the same comparison and the page hides
 * it, as it would in production.
 *
 * This is what `fakeAccounting` serves unless a test states a reconciliation, so that a test
 * about the invested figures stays a test about them: no notice, no table and no badge is
 * added to its screen.
 */
export function matchingReconciliation(positions: PositionsResponse): ReconciliationResponse {
  if (positions.computed_at === null) {
    return notReconciled({ exchanges: [], last_recompute: positions.last_recompute });
  }
  const held = positions.positions.filter((entry) => !new Exact(entry.quantity).isZero());

  return reconciliation({
    computed_at: positions.computed_at,
    last_recompute: positions.last_recompute,
    assets: held.map((entry) => matchedAsset(entry.asset, entry.quantity)),
    exchanges: held.length === 0 ? [] : [exchangeBalances()],
  });
}

function sameRecompute(a: LastRecomputeResponse | null, b: LastRecomputeResponse | null): boolean {
  if (a === null || b === null) {
    return a === b;
  }
  return a.at === b.at && a.outcome === b.outcome && a.error === b.error;
}

/**
 * Throws unless `response` and `positions` describe one snapshot: the history side of the
 * comparison **is** the snapshot's `Position.quantity` per asset (spec 025, "Design: service
 * and endpoint"), so the two endpoints cannot disagree about when it was computed, about what
 * an asset's history holds, or about which assets it holds. Nor about `last_recompute`, which
 * both serve from the one status the trigger keeps.
 */
export function assertSameSnapshot(
  response: ReconciliationResponse,
  positions: PositionsResponse,
): void {
  if (response.computed_at !== positions.computed_at) {
    fail(
      `computed_at is ${String(response.computed_at)}; the positions' snapshot was computed ` +
        `at ${String(positions.computed_at)}, and the history side is that snapshot.`,
    );
  }
  if (!sameRecompute(response.last_recompute, positions.last_recompute)) {
    fail(
      `last_recompute is ${JSON.stringify(response.last_recompute)}; the positions serve ` +
        `${JSON.stringify(positions.last_recompute)}, and both read one status.`,
    );
  }
  const compared = new Map(response.assets.map((entry) => [entry.asset, entry]));
  for (const entry of positions.positions) {
    const quantity = new Exact(entry.quantity);
    const found = compared.get(entry.asset);
    if (found === undefined) {
      if (!quantity.isZero()) {
        fail(`${entry.asset} is held in the positions (${entry.quantity}) and is not compared.`);
      }
      continue;
    }
    if (!new Exact(found.history_quantity).eq(quantity)) {
      fail(
        `${entry.asset} history_quantity is ${found.history_quantity}; the position holds ` +
          `${entry.quantity}.`,
      );
    }
    compared.delete(entry.asset);
  }
  for (const [asset, entry] of compared) {
    if (!new Exact(entry.history_quantity).isZero()) {
      fail(`${asset} history_quantity is ${entry.history_quantity}, and it has no position.`);
    }
  }
}

/*
 * Comparisons of the positions `accountingFixtures.ts` builds. The history side of each asset
 * is the quantity its position holds, which {@link assertSameSnapshot} checks when the pair is
 * handed to `fakeAccounting`.
 */

/**
 * BTC as `position()` holds it, and held beyond it. Hand-worked:
 *
 * - the history holds 1.5;
 * - the wallets hold 1.62345678 and the exchanges 0.25: held 1.62345678 + 0.25 = 1.87345678;
 * - difference 1.87345678 - 1.5 = +0.37345678;
 * - 0.37345678 x 100 = 37.345678 against 1 x max(1.5, 1.87345678) = 1.87345678: `history_short`.
 */
export function btcBeyondPosition(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'BTC',
    history_quantity: '1.500000000000000000',
    wallet_quantity: '1.623456780000000000',
    exchange_quantity: '0.250000000000000000',
    held_quantity: '1.873456780000000000',
    difference: '0.373456780000000000',
  });
}

export const BTC_BEYOND = {
  history: '1.500000000000000000',
  wallets: '1.623456780000000000',
  exchanges: '0.250000000000000000',
  held: '1.873456780000000000',
  difference: '0.373456780000000000',
} as const;

/**
 * BTC as `position()` holds it, with less read than the history accounts for. Hand-worked:
 * the history holds 1.5, the exchanges 1.0 and no wallet anything: held 1.0, difference
 * 1.0 - 1.5 = -0.5; 50 against 1.5: `history_over`.
 */
export function btcBelowPosition(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'BTC',
    history_quantity: '1.500000000000000000',
    wallet_quantity: ZERO,
    exchange_quantity: '1.000000000000000000',
    held_quantity: '1.000000000000000000',
    difference: '-0.500000000000000000',
    status: 'history_over',
  });
}

export const BTC_BELOW = {
  history: '1.500000000000000000',
  wallets: ZERO,
  exchanges: '1.000000000000000000',
  held: '1.000000000000000000',
  difference: '-0.500000000000000000',
} as const;

/**
 * ETH as `ethUnpriced()` holds it, and held beyond it, every one of 18 places in use.
 * Hand-worked:
 *
 * - the history holds 2.718281828459045235;
 * - the exchanges hold 3.141592653589793238, no wallet does: held 3.141592653589793238;
 * - difference 3.141592653589793238 - 2.718281828459045235 = +0.423310825130748003;
 * - 42.33... against 3.14...: `history_short`.
 *
 * As doubles the three are 2.718281828459045, 3.141592653589793 and 0.423310825130748.
 */
export function ethBeyondPosition(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'ETH',
    history_quantity: '2.718281828459045235',
    wallet_quantity: ZERO,
    exchange_quantity: '3.141592653589793238',
    held_quantity: '3.141592653589793238',
    difference: '0.423310825130748003',
  });
}

export const ETH_BEYOND = {
  history: '2.718281828459045235',
  wallets: ZERO,
  exchanges: '3.141592653589793238',
  held: '3.141592653589793238',
  difference: '0.423310825130748003',
} as const;

/**
 * KAS as `kasUnknownBasis()` holds it, a hair above the wallets and inside the tolerance.
 * Hand-worked: the history holds 1500, the wallets 1490: difference -10; 10 x 100 = 1000
 * against 1 x max(1500, 1490) = 1500: a match, with a difference that is not zero.
 */
export function kasWithinTolerance(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'KAS',
    history_quantity: '1500.000000000000000000',
    wallet_quantity: '1490.000000000000000000',
    exchange_quantity: ZERO,
    held_quantity: '1490.000000000000000000',
    difference: '-10.000000000000000000',
    status: 'match',
  });
}

/**
 * XRP as `xrpClosed()` leaves it: the history holds none - every unit it knew of was sold -
 * and an exchange still holds 12.5. Difference +12.5, `history_short`. Its position is closed,
 * so the positions table has no row for it.
 */
export function xrpLeftOnExchange(): AssetReconciliationResponse {
  return assetReconciliation({
    asset: 'XRP',
    history_quantity: ZERO,
    wallet_quantity: ZERO,
    exchange_quantity: '12.500000000000000000',
    held_quantity: '12.500000000000000000',
    difference: '12.500000000000000000',
  });
}

export const XRP_LEFT = {
  history: ZERO,
  wallets: ZERO,
  exchanges: '12.500000000000000000',
  held: '12.500000000000000000',
  difference: '12.500000000000000000',
} as const;

/**
 * The comparison that goes with `investedPortfolio()`, one asset of every kind:
 *
 * | Asset | Position | History | Wallets | Exchanges | Difference | Status |
 * |---|---|---|---|---|---|---|
 * | BGB | closed | 0 | 0 | 0 | - | left out: nothing on either side |
 * | BTC | held | 1.5 | 1.62345678 | 0.25 | +0.37345678 | `history_short` |
 * | ETH | held | 2.718281828459045235 | 0 | 3.141592653589793238 | +0.423310825130748003 | `history_short` |
 * | KAS | held | 1500 | 1490 | 0 | -10 | `match` |
 * | SOL | held | 10 | 0 | 7.5 | -2.5 | `history_over` |
 * | XRP | closed | 0 | 0 | 12.5 | +12.5 | `history_short` |
 *
 * Two venues were read, 16 and 14 minutes ago, and three wallets, the oldest 20 minutes ago.
 * Nothing is missing.
 */
export function investedPortfolioGaps(
  overrides: Partial<ReconciliationResponse> = {},
): ReconciliationResponse {
  return reconciliation({
    assets: [
      btcBeyondPosition(),
      ethBeyondPosition(),
      kasWithinTolerance(),
      solOver(),
      xrpLeftOnExchange(),
    ],
    exchanges: [
      exchangeBalances({ exchange_key: 'bingx', balances_read_at: OTHER_BALANCES_READ_AT }),
      exchangeBalances(),
    ],
    wallets: walletReadings(3),
    ...overrides,
  });
}
