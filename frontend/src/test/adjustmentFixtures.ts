import Decimal from 'decimal.js';

import type { components } from '@/api/generated/schema';

/**
 * Fixture builders for the manual adjustments (spec 023, shown by spec 027):
 * `GET/POST /api/accounting/adjustments`, `PUT/DELETE /api/accounting/adjustments/{id}` and
 * `GET /api/accounting/first-trades`.
 *
 * Typed with the generated OpenAPI types and, like `accountingFixtures.ts`, **checked against
 * what the backend can write**. An adjustment on the wire is the row as stored
 * (`services/adjustments.py`, `view_of`): its amounts are at the column's eighteen places, its
 * instants are in UTC as Pydantic serialises them, and it passed `validate_draft` when it was
 * written. A fixture with a quantity of `"1.5"`, an `occurred_at` with three fractional digits
 * or a blank note would test the page against a response no backend sends, so every builder
 * runs {@link assertWritableAdjustment} and refuses one.
 *
 * `decimal.js` is used here only to **check** a figure and to give it the stored scale, on a
 * private clone, and never through `@/lib/money`, the code under test. No amount here is a
 * JavaScript number, and none is anybody's real holding.
 */

type Schemas = components['schemas'];

export type AdjustmentResponse = Schemas['AdjustmentResponse'];
export type AdjustmentListResponse = Schemas['AdjustmentListResponse'];
export type AdjustmentCreateRequest = Schemas['AdjustmentCreateRequest'];
export type AdjustmentReplaceRequest = Schemas['AdjustmentReplaceRequest'];
export type FirstTradeResponse = Schemas['FirstTradeResponse'];
export type FirstTradesResponse = Schemas['FirstTradesResponse'];

/** The five fields an owner enters, as both requests carry them. */
export type AdjustmentBody = AdjustmentReplaceRequest;

/*
 * The exact sentences the backend answers with, copied from
 * `backend/src/portfolio/services/adjustments.py` and, for the engine's own refusals, from
 * `backend/src/portfolio/domain/accounting/events.py` with the class prefix removed as
 * `_engine_rule` removes it. Written out literally, so that a wording change on either side
 * shows up as a diff here. **None of them quotes a value.**
 */
export const ADJUSTMENT_NOT_FOUND_DETAIL = 'No adjustment with that id.';
export const ASSET_SYMBOL_RULE =
  'asset must be the symbol exactly as the exchange spells it: 1 to 20 upper-case letters ' +
  'or digits, such as BTC';
export const CASH_ASSET_RULE =
  'asset must not be a cash asset (USDC, USDT): cash is the unit of account, and an ' +
  'adjustment of it changes nothing';
export const OCCURRED_IN_FUTURE_RULE = 'occurred_at must not be later than now';
export const OCCURRED_NAIVE_RULE = 'occurred_at must be a timezone-aware datetime';
export const QUANTITY_NOT_POSITIVE_RULE = 'quantity must be greater than zero';
export const QUANTITY_TOO_PRECISE_RULE = 'quantity has more than 18 decimal places';
export const QUANTITY_TOO_LARGE_RULE = 'quantity has more than 20 digits before the decimal point';
export const UNIT_COST_NEGATIVE_RULE = 'unit_cost must not be negative';
export const UNIT_COST_TOO_PRECISE_RULE = 'unit_cost has more than 18 decimal places';
export const UNIT_COST_TOO_LARGE_RULE =
  'unit_cost has more than 20 digits before the decimal point';
export const TOTAL_COST_TOO_LARGE_RULE =
  'unit_cost times quantity has more than 20 digits before the decimal point';
export const NOTE_BLANK_RULE = 'note must not be blank';
export const NOTE_TOO_LONG_RULE = 'note must be at most 500 characters';

/** `NOTE_MAX_LENGTH`: counted as `len()` counts it, on the note as given. */
export const NOTE_MAX_LENGTH = 500;

/** The 422's own detail, from `api/errors.py`. */
export const VALIDATION_DETAIL = 'The request parameters failed validation.';

/** `REFUSAL_TYPE`: the `type` of a refusal by the service, in a 422's `errors`. */
export const REFUSAL_TYPE = 'value_error';

/** `DEFAULT_CASH_ASSETS`: an adjustment of one is refused, and first-trades leaves them out. */
export const CASH_ASSETS: readonly string[] = ['USDC', 'USDT'];

/** `ASSET_SYMBOL_PATTERN`. */
export const ASSET_SYMBOL_PATTERN = /^[A-Z0-9]{1,20}$/;

/** `ADJUSTMENT_SCALE`: the places an amount is stored at, and so served at. */
export const AMOUNT_SCALE = 18;

/** `MAX_AMOUNT_INTEGER_DIGITS`. */
export const MAX_AMOUNT_INTEGER_DIGITS = 20;

function fail(message: string): never {
  throw new Error(`Impossible adjustment fixture: ${message}`);
}

/**
 * A private `decimal.js` constructor, so that nothing here depends on - or changes - the
 * global precision `lib/money.ts` sets. Eighty digits hold the product of two amounts of
 * twenty integer digits and eighteen places exactly.
 */
export const Exact = Decimal.clone({ precision: 80, rounding: Decimal.ROUND_HALF_EVEN });
type Exact = Decimal;

const PLAIN_DECIMAL = /^-?\d+(?:\.(\d+))?$/;

/** Parses a wire amount, refusing one that is not at the stored scale. */
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

/** An amount as `NumericText(ADJUSTMENT_SCALE)` stores it: eighteen places, and one zero. */
export function asStoredAmount(value: string): string {
  const stored = new Exact(value).toFixed(AMOUNT_SCALE);
  return new Exact(stored).isZero() ? new Exact(0).toFixed(AMOUNT_SCALE) : stored;
}

const LIMIT = new Exact(10).pow(MAX_AMOUNT_INTEGER_DIGITS);

/**
 * A UTC instant as Pydantic serialises an aware `datetime`: `Z`, and either no fractional
 * part (no microseconds) or exactly six digits. Never three: that is JavaScript's spelling,
 * and a stored instant that looks like one is the fixture a round trip through `Date` would
 * not change - which is the very thing an edit must be shown not to do.
 */
const WIRE_INSTANT = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{6}))?Z$/;

/**
 * An instant as a string that orders as the instant does: the fraction is written out to six
 * digits, so that `...:37Z` sorts before `...:37.123456Z`, which their own spellings do not.
 */
export function instantKey(label: string, instant: string): string {
  const match = WIRE_INSTANT.exec(instant);
  if (match === null) {
    fail(
      `${label} "${instant}" is not a UTC instant as the backend serialises one: ` +
        'YYYY-MM-DDTHH:MM:SSZ, or with exactly six fractional digits.',
    );
  }
  const whole = match[1] ?? '';
  if (Number.isNaN(Date.parse(`${whole}Z`))) {
    fail(`${label} "${instant}" names no real instant.`);
  }
  const fraction = match[2] ?? '000000';
  if (match[2] === '000000') {
    fail(`${label} "${instant}": a datetime with no microseconds is serialised without them.`);
  }
  return `${whole}.${fraction}`;
}

/**
 * An ISO 8601 instant with an offset, as a request carries it, in the form it is stored and
 * served in: UTC, `Z`, microseconds only when there are some. The fraction is carried over as
 * text, so nothing below a millisecond is lost to `Date`.
 *
 * Returns `null` for text `datetime.fromisoformat` would refuse, and `'naive'` for a datetime
 * with no offset, which it parses and the engine then refuses.
 */
export function asStoredInstant(iso: string): string | null {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})?$/.exec(
    iso,
  );
  if (match === null) {
    return null;
  }
  const zone = match[3];
  if (zone === undefined) {
    return 'naive';
  }
  const epoch = Date.parse(`${match[1] ?? ''}${zone}`);
  if (Number.isNaN(epoch)) {
    return null;
  }
  const fraction = (match[2] ?? '').padEnd(6, '0');
  const whole = new Date(epoch).toISOString().slice(0, 19);
  return fraction === '000000' ? `${whole}Z` : `${whole}.${fraction}Z`;
}

/** Throws unless `entry` is an adjustment `validate_draft` accepted and `view_of` serves. */
export function assertWritableAdjustment(entry: AdjustmentResponse): AdjustmentResponse {
  const label = `adjustment ${String(entry.id)}`;

  if (!Number.isSafeInteger(entry.id) || entry.id < 1) {
    fail(`${label}: an id is a positive integer; AUTOINCREMENT starts at 1.`);
  }
  if (!ASSET_SYMBOL_PATTERN.test(entry.asset)) {
    fail(`${label}: asset "${entry.asset}" is not 1 to 20 upper-case letters or digits.`);
  }
  if (CASH_ASSETS.includes(entry.asset)) {
    fail(`${label}: an adjustment of the cash asset ${entry.asset} is refused.`);
  }

  const quantity = exact(`${label} quantity`, entry.quantity);
  if (quantity.lte(0)) {
    fail(`${label}: quantity is greater than zero.`);
  }
  if (quantity.abs().gte(LIMIT)) {
    fail(`${label}: quantity has at most 20 digits before the point.`);
  }
  if (entry.unit_cost !== null) {
    const unitCost = exact(`${label} unit_cost`, entry.unit_cost);
    if (unitCost.lt(0)) {
      fail(`${label}: unit_cost is zero or more, or null for unknown.`);
    }
    if (entry.unit_cost.startsWith('-')) {
      fail(`${label}: a zero is stored and served unsigned.`);
    }
    if (unitCost.gte(LIMIT) || unitCost.times(quantity).gte(LIMIT)) {
      fail(`${label}: unit_cost, and unit_cost times quantity, have at most 20 integer digits.`);
    }
  }

  const occurred = instantKey(`${label} occurred_at`, entry.occurred_at);
  const created = instantKey(`${label} created_at`, entry.created_at);
  const updated = instantKey(`${label} updated_at`, entry.updated_at);
  if (updated < created) {
    fail(`${label}: it was last written at or after it was created.`);
  }
  if (occurred > updated) {
    fail(`${label}: occurred_at was not later than now when it was last written.`);
  }

  if (entry.note.trim() === '') {
    fail(`${label}: a note is required, and not blank.`);
  }
  if (Array.from(entry.note).length > NOTE_MAX_LENGTH) {
    fail(`${label}: a note has at most ${String(NOTE_MAX_LENGTH)} characters.`);
  }

  return entry;
}

/** The order `AdjustmentService.list` serves: by `occurred_at`, then by id. */
export function inReplayOrder(entries: readonly AdjustmentResponse[]): AdjustmentResponse[] {
  return [...entries].sort((a, b) => {
    const left = instantKey('occurred_at', a.occurred_at);
    const right = instantKey('occurred_at', b.occurred_at);
    if (left !== right) {
      return left < right ? -1 : 1;
    }
    return a.id - b.id;
  });
}

/** Throws unless `entries` is a list `GET /api/accounting/adjustments` can serve, in order. */
export function assertWritableAdjustments(
  entries: readonly AdjustmentResponse[],
): readonly AdjustmentResponse[] {
  entries.forEach(assertWritableAdjustment);
  const ids = entries.map((entry) => entry.id);
  if (new Set(ids).size !== ids.length) {
    fail('an id names one adjustment.');
  }
  const ordered = inReplayOrder(entries).map((entry) => entry.id);
  if (ordered.join() !== ids.join()) {
    fail(
      `the list is served by occurred_at and then id: ${ordered.join(', ')}, not ` +
        `${ids.join(', ')}.`,
    );
  }
  return entries;
}

/** Throws unless `response` is one `GET /api/accounting/first-trades` can serve. */
export function assertWritableFirstTrades(response: FirstTradesResponse): FirstTradesResponse {
  const names = response.assets.map((entry) => entry.asset);
  // Code-point order, as Python sorts `str`: `<` on two strings of ASCII symbols is that.
  if (names.some((name, index) => index > 0 && (names[index - 1] ?? '') >= name)) {
    fail('first-trades lists one entry per asset, sorted by asset.');
  }
  for (const entry of response.assets) {
    if (entry.asset === '') {
      fail('first-trades: an asset has a name.');
    }
    if (CASH_ASSETS.includes(entry.asset)) {
      fail(`first-trades leaves the cash asset ${entry.asset} out.`);
    }
    instantKey(`${entry.asset} first_trade_at`, entry.first_trade_at);
  }
  return response;
}

/*
 * Instants. The page tests that fake the clock run under `NOW` (2026-09-24T12:00:00Z), so
 * every adjustment below was acquired, created and updated before it.
 */

/** When the fixtures' adjustments were recorded: a clock reading, so with microseconds. */
export const ADJUSTMENT_CREATED_AT = '2026-09-20T09:00:00.482913Z';
/** When one of them was edited afterwards. */
export const ADJUSTMENT_UPDATED_AT = '2026-09-21T18:30:12.007731Z';

/**
 * A stored instant that no round trip through `Date` reproduces: it has seconds, and it has
 * microseconds, which `Date` cannot hold and `toISOString` cannot spell. An edit that leaves
 * the date alone must send exactly these bytes (spec 027, "What is sent").
 */
export const BTC_OPENING_AT = '2025-02-28T23:59:37.123456Z';
/** A stored instant on a whole second, served with no fractional part. */
export const KAS_ACQUIRED_AT = '2025-06-01T12:00:05Z';
/** A stored instant on a whole minute: the one an untouched edit could round-trip unnoticed. */
export const ETH_ACQUIRED_AT = '2025-08-15T08:30:00Z';

/** BTC's opening balance, as stored. Unit cost known. */
export const BTC_OPENING = {
  id: 1,
  asset: 'BTC',
  quantity: '1.500000000000000000',
  unit_cost: '20000.000000000000000000',
  occurred_at: BTC_OPENING_AT,
  note: 'Opening balance: bought before the exchange history begins.',
} as const;

/** KAS acquired off an exchange, at a cost nobody wrote down: `null`, which is not zero. */
export const KAS_UNKNOWN_COST = {
  id: 2,
  asset: 'KAS',
  quantity: '12000.000000000000000000',
  unit_cost: null,
  occurred_at: KAS_ACQUIRED_AT,
  note: 'Mined before any exchange was used. Cost not recorded.',
} as const;

/**
 * ETH with every one of eighteen places in use, in both amounts. As doubles these are
 * 3.141592653589793 and 1234.5678901234568: a page that parsed either would show it.
 */
export const ETH_PRECISE = {
  id: 3,
  asset: 'ETH',
  quantity: '3.141592653589793238',
  unit_cost: '1234.567890123456789012',
  occurred_at: ETH_ACQUIRED_AT,
  note: 'Bought from a friend, paid in cash.',
} as const;

/** One adjustment, checked. The default is {@link BTC_OPENING}. */
export function adjustment(overrides: Partial<AdjustmentResponse> = {}): AdjustmentResponse {
  return assertWritableAdjustment({
    ...BTC_OPENING,
    created_at: ADJUSTMENT_CREATED_AT,
    updated_at: ADJUSTMENT_CREATED_AT,
    ...overrides,
  });
}

/** KAS at an unknown cost. */
export function kasUnknownCost(overrides: Partial<AdjustmentResponse> = {}): AdjustmentResponse {
  return adjustment({ ...KAS_UNKNOWN_COST, ...overrides });
}

/** ETH at eighteen places. */
export function ethPrecise(overrides: Partial<AdjustmentResponse> = {}): AdjustmentResponse {
  return adjustment({ ...ETH_PRECISE, ...overrides });
}

/**
 * The owner's adjustments for most tests, in the endpoint's order: BTC (February 2025), KAS
 * (June 2025) and ETH (August 2025).
 */
export function threeAdjustments(): AdjustmentResponse[] {
  return [adjustment(), kasUnknownCost(), ethPrecise()];
}

/** A list response, checked: the entries and their order. */
export function adjustmentList(entries: readonly AdjustmentResponse[]): AdjustmentListResponse {
  return { adjustments: [...assertWritableAdjustments(entries)] };
}

/** The five fields of `entry`, as a `PUT` that changes nothing would carry them. */
export function bodyOf(entry: AdjustmentResponse): AdjustmentBody {
  return {
    asset: entry.asset,
    quantity: entry.quantity,
    unit_cost: entry.unit_cost,
    occurred_at: entry.occurred_at,
    note: entry.note,
  };
}

/*
 * First trades. Exchange fills are stamped to the millisecond, so a fill's time carries a
 * fraction more often than not; `BTC`'s below is the spec's own example.
 */

/** The earliest imported BTC fill: the instant spec 027 prints. */
export const BTC_FIRST_TRADE_AT = '2025-03-01T10:00:37Z';
export const ETH_FIRST_TRADE_AT = '2025-07-04T16:45:12.250000Z';
export const KAS_FIRST_TRADE_AT = '2025-11-02T05:30:00Z';

export function firstTrade(
  asset: string,
  firstTradeAt: string = BTC_FIRST_TRADE_AT,
): FirstTradeResponse {
  return { asset, first_trade_at: firstTradeAt };
}

/** A first-trades response, checked. The default is an owner with no fills: an empty list. */
export function firstTrades(entries: readonly FirstTradeResponse[] = []): FirstTradesResponse {
  return assertWritableFirstTrades({ assets: [...entries] });
}

/** BTC, ETH and KAS, each with the instant its imported history begins. */
export function threeFirstTrades(): FirstTradesResponse {
  return firstTrades([
    firstTrade('BTC', BTC_FIRST_TRADE_AT),
    firstTrade('ETH', ETH_FIRST_TRADE_AT),
    firstTrade('KAS', KAS_FIRST_TRADE_AT),
  ]);
}
