import Decimal from 'decimal.js';

import type { components } from '@/api/generated/schema';

import type { ExchangeKey } from './exchangeFixtures';

/**
 * Fixture builders for `GET /api/exchanges/fills` (spec 024).
 *
 * **Checked against what the backend can write**, like `exchangeFixtures.ts` and
 * `accountingFixtures.ts`, and for the same reason: spec 011's closing section, where 467
 * tests passed over a response no backend code path produces. The rules are read from the
 * code that writes each field:
 *
 * - a row is one `exchange_fills` row, which `NormalizedFill` (`providers/exchanges/base.py`)
 *   admitted: every amount at most 18 places (`FILL_SCALE`), `quantity`, `price` and
 *   `quote_quantity` above zero, a null `fee_asset` only beside a zero fee, a base asset
 *   that is not the quote asset, and a fee that leaves a leg to account for
 *   (`trade_shape_problem`, spec 020);
 * - an amount goes out at the stored scale, so with exactly 18 places, as `NumericText`
 *   reads it back and `format(value, "f")` writes it;
 * - `usdt_value` is `quote_quantity` when the quote is `USDT`, else null. It is derived,
 *   never stated, so a builder cannot disagree with it.
 *
 * **Totals are derived from the rows, never written by hand.** {@link fillTotals} re-states
 * the spec's rules for `domain/fill_totals.py` over a private `decimal.js` clone, and the
 * fake serves exactly what it returns for the filtered set. The figures a test asserts are
 * still written out by hand beside each scenario, so an expectation is never the output of
 * the code that checks it, and never of `@/lib/money`, the code under test.
 *
 * No figure here is a JavaScript number, no fixture carries an address or a credential, and
 * order ids are synthetic digit strings.
 */

type Schemas = components['schemas'];

export type FillSide = Schemas['FillSide'];
export type ExchangeFill = Schemas['ExchangeFillResponse'];
export type FillTotals = Schemas['ExchangeFillTotalsResponse'];
export type FillsPageResponse = Schemas['ExchangeFillListResponse'];

const FILL_SIDES_RECORD: Record<FillSide, true> = { buy: true, sell: true };
export const ALL_FILL_SIDES = Object.keys(FILL_SIDES_RECORD) as FillSide[];

/** The only quote asset with a USDT value. */
export const USDT = 'USDT';

/** `db.models.FILL_SCALE`: every amount on the wire has exactly this many places. */
export const FILL_SCALE = 18;

/** The endpoint's page-size ceiling, `MAX_FILLS_LIMIT`, and its default. */
export const MAX_FILLS_LIMIT = 200;
export const DEFAULT_FILLS_LIMIT = 50;

function fail(message: string): never {
  throw new Error(`Impossible fill fixture: ${message}`);
}

/**
 * A private `decimal.js` constructor, so nothing here depends on, or changes, the global
 * precision `lib/money.ts` sets. Sums of 18-place amounts are exact at any length the
 * backend's `money.add` handles; 200 digits is far past anything a fixture holds.
 */
const Exact = Decimal.clone({ precision: 200, rounding: Decimal.ROUND_HALF_EVEN });
type Exact = Decimal;

const PLAIN_DECIMAL = /^-?\d+(?:\.(\d+))?$/;

/**
 * `value` written with exactly 18 places, as the backend serialises a stored amount.
 * `"0.5"` becomes `"0.500000000000000000"`. Refuses more than 18 places: `NormalizedFill`
 * refuses such an amount before it is ever stored.
 */
export function at18(value: string): string {
  const match = PLAIN_DECIMAL.exec(value);
  if (match === null) {
    fail(`"${value}" is not a plain decimal string.`);
  }
  if ((match[1]?.length ?? 0) > FILL_SCALE) {
    fail(`"${value}" has more than ${String(FILL_SCALE)} places; NormalizedFill refuses it.`);
  }
  return normalised(new Exact(value).toFixed(FILL_SCALE));
}

/** `_as_fixed_point`: a zero goes out unsigned. */
function normalised(fixed: string): string {
  return fixed.startsWith('-') && new Exact(fixed).isZero() ? fixed.slice(1) : fixed;
}

/** Zero at the stored scale. */
export const ZERO_18 = at18('0');

function exact(label: string, value: string): Exact {
  const match = PLAIN_DECIMAL.exec(value);
  if (match === null) {
    fail(`${label} "${value}" is not a plain decimal string.`);
  }
  if ((match[1]?.length ?? 0) !== FILL_SCALE) {
    fail(`${label} "${value}" does not have the ${String(FILL_SCALE)} places the backend sends.`);
  }
  return new Exact(value);
}

function wire(value: Exact): string {
  return normalised(value.toFixed(FILL_SCALE));
}

/** The instant an ISO string names, in milliseconds. Fixtures never carry sub-millisecond digits. */
export function instantOf(iso: string): number {
  if (!/(?:Z|[+-]\d{2}:\d{2})$/.test(iso)) {
    fail(`"${iso}" has no offset; every datetime this API serves is aware.`);
  }
  const parsed = Date.parse(iso.replace(/(\.\d{3})\d+/, '$1'));
  if (Number.isNaN(parsed)) {
    fail(`"${iso}" is not an ISO 8601 instant.`);
  }
  return parsed;
}

/**
 * How Pydantic writes an aware UTC `datetime`: `Z`, and a fraction only when there are
 * microseconds, then always six digits.
 */
const PYDANTIC_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?Z$/;

/** `trade_shape_problem`: what leaves a trade with no leg to account for. */
function shapeProblem(f: ExchangeFill): string | null {
  if (f.base_asset === f.quote_asset) {
    return 'base_asset and quote_asset must be different assets';
  }
  const fee = new Exact(f.fee_amount);
  if (fee.isZero() || f.fee_asset === null) {
    return null;
  }
  const buy = f.side === 'buy';
  const receivedAsset = buy ? f.base_asset : f.quote_asset;
  const givenAsset = buy ? f.quote_asset : f.base_asset;
  const received = new Exact(buy ? f.quantity : f.quote_quantity);
  const given = new Exact(buy ? f.quote_quantity : f.quantity);
  if (f.fee_asset === receivedAsset && received.minus(fee).lte(0)) {
    return 'a fee in the asset received must leave something received';
  }
  if (f.fee_asset === givenAsset && given.plus(fee).lte(0)) {
    return 'a rebate in the asset given must leave something given';
  }
  return null;
}

/** Throws unless `f` is a row the endpoint can serve. */
export function assertWritableFill(f: ExchangeFill): ExchangeFill {
  const label = `fill ${String(f.id)}`;

  if (!Number.isInteger(f.id) || f.id < 1) {
    fail(`${label}: the id is an INTEGER PRIMARY KEY, so a positive integer.`);
  }
  if (!PYDANTIC_UTC.test(f.executed_at)) {
    fail(`${label}: executed_at "${f.executed_at}" is not how the API writes a UTC instant.`);
  }
  for (const [field, value] of [
    ['symbol', f.symbol],
    ['base_asset', f.base_asset],
    ['quote_asset', f.quote_asset],
  ] as const) {
    if (value.trim() === '') {
      fail(`${label}: ${field} is never blank.`);
    }
  }
  if (!(ALL_FILL_SIDES as readonly string[]).includes(f.side)) {
    fail(`${label}: side is a FillSide.`);
  }
  for (const [field, value] of [
    ['quantity', f.quantity],
    ['price', f.price],
    ['quote_quantity', f.quote_quantity],
  ] as const) {
    if (exact(`${label} ${field}`, value).lte(0)) {
      fail(`${label}: ${field} is above zero.`);
    }
  }
  const fee = exact(`${label} fee_amount`, f.fee_amount);
  if (f.fee_asset === null && !fee.isZero()) {
    fail(`${label}: a null fee_asset is only ever beside a zero fee.`);
  }
  if (f.fee_asset?.trim() === '') {
    fail(`${label}: a fee asset is never blank.`);
  }
  const expectedUsdt = f.quote_asset === USDT ? f.quote_quantity : null;
  if (f.usdt_value !== expectedUsdt) {
    fail(`${label}: usdt_value is quote_quantity for a USDT quote, and null otherwise.`);
  }
  const problem = shapeProblem(f);
  if (problem !== null) {
    fail(`${label}: ${problem} (spec 020).`);
  }
  return f;
}

export interface FillInput {
  readonly id: number;
  readonly executed_at: string;
  readonly exchange_key?: ExchangeKey;
  readonly base_asset?: string;
  readonly quote_asset?: string;
  /** The venue's spelling. Defaults to Bitget's `BTCUSDT` and BingX's `BTC-USDT`. */
  readonly symbol?: string;
  readonly side?: FillSide;
  /** Plain decimals, padded to 18 places. */
  readonly quantity?: string;
  readonly price?: string;
  /** Defaults to `quantity x price`, a value a venue could have reported. */
  readonly quote_quantity?: string;
  readonly quote_quantity_derived?: boolean;
  readonly fee_amount?: string;
  readonly fee_asset?: string | null;
  readonly order_id?: string | null;
}

function symbolOf(key: ExchangeKey, base: string, quote: string): string {
  return key === 'bingx' ? `${base}-${quote}` : `${base}${quote}`;
}

/**
 * One row, checked by {@link assertWritableFill}. A BTC/USDT buy on Bitget by default, with
 * a fee in the asset received.
 */
export function fill(input: FillInput): ExchangeFill {
  const key = input.exchange_key ?? 'bitget';
  const base = input.base_asset ?? 'BTC';
  const quote = input.quote_asset ?? USDT;
  const quantity = at18(input.quantity ?? '0.5');
  const price = at18(input.price ?? '60000');
  const quoteQuantity =
    input.quote_quantity === undefined
      ? wire(new Exact(quantity).times(new Exact(price)))
      : at18(input.quote_quantity);
  const feeAsset = input.fee_asset === undefined ? base : input.fee_asset;

  return assertWritableFill({
    id: input.id,
    executed_at: input.executed_at,
    exchange_key: key,
    symbol: input.symbol ?? symbolOf(key, base, quote),
    base_asset: base,
    quote_asset: quote,
    side: input.side ?? 'buy',
    quantity,
    price,
    quote_quantity: quoteQuantity,
    quote_quantity_derived: input.quote_quantity_derived ?? false,
    usdt_value: quote === USDT ? quoteQuantity : null,
    fee_amount: at18(input.fee_amount ?? (feeAsset === null ? '0' : '0.0005')),
    fee_asset: feeAsset,
    order_id: input.order_id === undefined ? `10000${String(input.id)}` : input.order_id,
  });
}

/** Refuses a set of rows the table cannot hold: two rows with one id. */
export function assertWritableFills(fills: readonly ExchangeFill[]): readonly ExchangeFill[] {
  const ids = fills.map((entry) => entry.id);
  if (new Set(ids).size !== ids.length) {
    fail('two rows share an id.');
  }
  fills.forEach(assertWritableFill);
  return fills;
}

/** Newest first by `executed_at`, ties broken by `id` descending: the endpoint's order. */
export function newestFirst(fills: readonly ExchangeFill[]): ExchangeFill[] {
  return [...fills].sort(
    (a, b) => instantOf(b.executed_at) - instantOf(a.executed_at) || b.id - a.id,
  );
}

function byKey<T>(entries: Map<string, T>): [string, T][] {
  // Python's `sorted` on `str` is by code point; for the ASCII tickers here that is
  // what `<` on strings gives too.
  return [...entries.entries()].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
}

interface AssetAccumulator {
  count: number;
  bought: Exact;
  sold: Exact;
  usdtSpent: Exact;
  usdtReceived: Exact;
  unvalued: number;
}

interface QuoteAccumulator {
  count: number;
  spent: Exact;
  received: Exact;
}

/**
 * The totals over `fills`, by the rules of spec 024's "Per-asset figures", "Every net" and
 * "Fees": plain signed sums, nets as buys minus sells, never clamped, and quantities never
 * summed across assets.
 */
export function fillTotals(fills: readonly ExchangeFill[]): FillTotals {
  const zero = new Exact(0);
  const assets = new Map<string, AssetAccumulator>();
  const quotes = new Map<string, QuoteAccumulator>();
  const fees = new Map<string, Exact>();
  let usdtSpent = zero;
  let usdtReceived = zero;
  let unvalued = 0;

  for (const f of fills) {
    const buy = f.side === 'buy';
    const quantity = new Exact(f.quantity);
    const quoteQuantity = new Exact(f.quote_quantity);
    const valued = f.quote_asset === USDT;

    const asset = assets.get(f.base_asset) ?? {
      count: 0,
      bought: zero,
      sold: zero,
      usdtSpent: zero,
      usdtReceived: zero,
      unvalued: 0,
    };
    asset.count += 1;
    if (buy) {
      asset.bought = asset.bought.plus(quantity);
    } else {
      asset.sold = asset.sold.plus(quantity);
    }
    if (valued) {
      if (buy) {
        asset.usdtSpent = asset.usdtSpent.plus(quoteQuantity);
        usdtSpent = usdtSpent.plus(quoteQuantity);
      } else {
        asset.usdtReceived = asset.usdtReceived.plus(quoteQuantity);
        usdtReceived = usdtReceived.plus(quoteQuantity);
      }
    } else {
      asset.unvalued += 1;
      unvalued += 1;
      const quote = quotes.get(f.quote_asset) ?? { count: 0, spent: zero, received: zero };
      quote.count += 1;
      if (buy) {
        quote.spent = quote.spent.plus(quoteQuantity);
      } else {
        quote.received = quote.received.plus(quoteQuantity);
      }
      quotes.set(f.quote_asset, quote);
    }
    assets.set(f.base_asset, asset);

    if (f.fee_asset !== null) {
      fees.set(f.fee_asset, (fees.get(f.fee_asset) ?? zero).plus(new Exact(f.fee_amount)));
    }
  }

  return {
    fill_count: fills.length,
    by_asset: byKey(assets).map(([asset, totals]) => ({
      asset,
      fill_count: totals.count,
      bought: wire(totals.bought),
      sold: wire(totals.sold),
      net: wire(totals.bought.minus(totals.sold)),
      usdt_spent: wire(totals.usdtSpent),
      usdt_received: wire(totals.usdtReceived),
      usdt_net: wire(totals.usdtSpent.minus(totals.usdtReceived)),
      usdt_unvalued_fill_count: totals.unvalued,
    })),
    usdt: {
      spent: wire(usdtSpent),
      received: wire(usdtReceived),
      net: wire(usdtSpent.minus(usdtReceived)),
    },
    not_valued_in_usdt: {
      fill_count: unvalued,
      by_quote_asset: byKey(quotes).map(([quoteAsset, totals]) => ({
        quote_asset: quoteAsset,
        fill_count: totals.count,
        spent: wire(totals.spent),
        received: wire(totals.received),
        net: wire(totals.spent.minus(totals.received)),
      })),
    },
    fees: byKey(fees).map(([asset, amount]) => ({ asset, amount: wire(amount) })),
  };
}

export interface FillQuery {
  /** Empty means every venue. */
  readonly exchanges: readonly ExchangeKey[];
  /** Inclusive, in milliseconds. */
  readonly from: number | null;
  /** Exclusive, in milliseconds. */
  readonly to: number | null;
  readonly limit: number;
  readonly offset: number;
}

/**
 * What the endpoint answers for `query` over the owner's `fills`: the filtered set newest
 * first, the page `limit` and `offset` cut from it, and totals over the whole filtered set.
 */
export function fillsPage(fills: readonly ExchangeFill[], query: FillQuery): FillsPageResponse {
  const matching = newestFirst(fills).filter((entry) => {
    const at = instantOf(entry.executed_at);
    return (
      (query.exchanges.length === 0 || query.exchanges.includes(entry.exchange_key)) &&
      (query.from === null || at >= query.from) &&
      (query.to === null || at < query.to)
    );
  });

  return {
    fills: matching.slice(query.offset, query.offset + query.limit),
    total_count: matching.length,
    totals: fillTotals(matching),
  };
}

/*
 * Scenario rows. Every instant is before `NOW` (2026-09-24T12:00:00Z).
 */

/**
 * Five fills in March 2026 over both venues, newest last here and newest first on the wire.
 * Between them they hold every case the totals and the table must tell apart. The totals
 * they produce are written out by hand in `fillFixtures.test.ts`, and a page test asserts
 * the same figures.
 *
 * | id  | venue  | pair     | side | quantity | price  | quote value      | fee             | order |
 * |-----|--------|----------|------|----------|--------|------------------|-----------------|-------|
 * | 101 | Bitget | BTC/USDT | buy  | 0.5      | 60000  | 30000 USDT       | 0.0005 BTC      | 5001  |
 * | 102 | Bitget | BTC/USDT | sell | 0.75     | 62000  | 46500 USDT       | 12.5 USDT       | none  |
 * | 103 | BingX  | ETH/USDC | buy  | 2        | 3000   | 6000 USDC, derived | -0.3 USDC (rebate) | 7003 |
 * | 104 | BingX  | ETH/USDT | buy  | 1        | 3100   | 3100 USDT        | 0.001 ETH       | 7004  |
 * | 105 | Bitget | SOL/BTC  | sell | 10       | 0.0025 | 0.025 BTC        | 0.00001 BTC     | 5005  |
 */
export function marchFills(): ExchangeFill[] {
  return [
    fill({
      id: 101,
      exchange_key: 'bitget',
      executed_at: '2026-03-02T09:15:00Z',
      quantity: '0.5',
      price: '60000',
      fee_amount: '0.0005',
      fee_asset: 'BTC',
      order_id: '5001',
    }),
    fill({
      id: 102,
      exchange_key: 'bitget',
      executed_at: '2026-03-10T14:00:00Z',
      side: 'sell',
      quantity: '0.75',
      price: '62000',
      fee_amount: '12.5',
      fee_asset: USDT,
      order_id: null,
    }),
    fill({
      id: 103,
      exchange_key: 'bingx',
      executed_at: '2026-03-15T08:00:00Z',
      base_asset: 'ETH',
      quote_asset: 'USDC',
      quantity: '2',
      price: '3000',
      quote_quantity_derived: true,
      fee_amount: '-0.3',
      fee_asset: 'USDC',
      order_id: '7003',
    }),
    fill({
      id: 104,
      exchange_key: 'bingx',
      executed_at: '2026-03-20T20:30:00.250000Z',
      base_asset: 'ETH',
      quantity: '1',
      price: '3100',
      fee_amount: '0.001',
      fee_asset: 'ETH',
      order_id: '7004',
    }),
    fill({
      id: 105,
      exchange_key: 'bitget',
      executed_at: '2026-03-25T11:00:00Z',
      base_asset: 'SOL',
      quote_asset: 'BTC',
      side: 'sell',
      quantity: '10',
      price: '0.0025',
      fee_amount: '0.00001',
      fee_asset: 'BTC',
      order_id: '5005',
    }),
  ];
}

/**
 * `count` USDT-quoted BTC buys on `key`, one a minute from `start`, ids from `firstId`:
 * enough rows to page through, each one different in its order id.
 */
export function manyFills(
  count: number,
  options: { readonly key?: ExchangeKey; readonly firstId?: number; readonly start?: string } = {},
): ExchangeFill[] {
  const start = instantOf(options.start ?? '2026-09-01T00:00:00Z');
  const firstId = options.firstId ?? 1;
  return Array.from({ length: count }, (_, index) =>
    fill({
      id: firstId + index,
      exchange_key: options.key ?? 'bitget',
      executed_at: new Date(start + index * 60_000).toISOString().replace('.000Z', 'Z'),
      quantity: '0.001',
      price: '60000',
    }),
  );
}
