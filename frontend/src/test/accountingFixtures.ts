import Decimal from 'decimal.js';

import type { components } from '@/api/generated/schema';

import { NOW, PRICE_AS_OF, STALE_PRICE_AS_OF } from './fixtures';

/**
 * Fixture builders for `GET /api/accounting/positions` (spec 021, rendered by spec 022).
 *
 * Typed with the generated OpenAPI types, like `fixtures.ts`, and - like
 * `exchangeFixtures.ts` - **checked against what the backend can write**. Spec 011's
 * closing lesson applies with more force here, because every figure is derived from others:
 * a fixture whose market value is not its price times its quantity, or whose totals do not
 * add up, tests a page against a response no backend sends. So every builder below runs
 * {@link assertWritablePositions}, which re-derives each figure from the rules in
 * `domain/accounting/valuation.py` and refuses a response that disagrees.
 *
 * The expected figures in the tests are still written out by hand, in the comments beside
 * each scenario. `decimal.js` is used here only to **check** them, on a private clone, never
 * to produce an expectation a test asserts - and never through `@/lib/money`, the code under
 * test.
 *
 * Every amount and quantity is an 18-place string, the scale the engine carries
 * (`VALUE_SCALE`); a price is at 12 places (`PRICE_SCALE`), and a percentage at 4
 * (`RETURN_PCT_SCALE`). Those scales are part of the check, so that a `<data value>`
 * assertion on one of these strings proves the page kept every trailing zero the backend
 * sent. No figure here is a JavaScript number.
 */

type Schemas = components['schemas'];

export type PositionsResponse = Schemas['PositionsResponse'];
export type AccountingPositionResponse = Schemas['AccountingPositionResponse'];
export type AccountingTotalsResponse = Schemas['AccountingTotalsResponse'];
export type ExclusionResponse = Schemas['ExclusionResponse'];
export type ExclusionReason = Schemas['ExclusionReason'];
export type AccountingWarningResponse = Schemas['AccountingWarningResponse'];
export type AccountingWarningKind = Schemas['AccountingWarningKind'];
export type LastRecomputeResponse = Schemas['LastRecomputeResponse'];
export type RecomputeOutcome = Schemas['RecomputeOutcome'];
export type PositionFlag = Schemas['PositionFlag'];
export type PriceUnavailable = Schemas['PriceUnavailable'];
export type ValueUnavailable = Schemas['ValueUnavailable'];
export type MarketValueUnavailable = PriceUnavailable | ValueUnavailable;
export type AccountingPrice = Schemas['PriceResponse'];

/*
 * Every member of every union, as a `Record` whose values are ignored. A member added on the
 * backend fails `tsc` here until a test names it.
 */

const POSITION_FLAGS_RECORD: Record<PositionFlag, true> = {
  history_incomplete: true,
  unattributed_fee: true,
  unknown_basis: true,
};
export const ALL_POSITION_FLAGS = Object.keys(POSITION_FLAGS_RECORD) as PositionFlag[];

const EXCLUSION_REASONS_RECORD: Record<ExclusionReason, true> = {
  unknown_basis: true,
  unpriced: true,
};
export const ALL_EXCLUSION_REASONS = Object.keys(EXCLUSION_REASONS_RECORD) as ExclusionReason[];

const PRICE_UNAVAILABLE_RECORD: Record<PriceUnavailable, true> = {
  every_source_failed: true,
  never_fetched: true,
  no_source_configured: true,
  unsupported_pair: true,
};
export const ALL_PRICE_UNAVAILABLE = Object.keys(PRICE_UNAVAILABLE_RECORD) as PriceUnavailable[];

const VALUE_UNAVAILABLE_RECORD: Record<ValueUnavailable, true> = { value_out_of_range: true };
export const ALL_MARKET_VALUE_UNAVAILABLE: readonly MarketValueUnavailable[] = [
  ...ALL_PRICE_UNAVAILABLE,
  ...(Object.keys(VALUE_UNAVAILABLE_RECORD) as ValueUnavailable[]),
];

const RECOMPUTE_OUTCOMES_RECORD: Record<RecomputeOutcome, true> = {
  failed: true,
  unchanged: true,
  written: true,
};
export const ALL_RECOMPUTE_OUTCOMES = Object.keys(RECOMPUTE_OUTCOMES_RECORD) as RecomputeOutcome[];

/*
 * Instants. Every page test runs under `NOW` (2026-09-24T12:00:00Z). The ones a relative
 * time is read from sit on whole minutes, so the phrase does not depend on a rounding mode.
 */

/** When the snapshot was written: 15 minutes before `NOW`. */
export const COMPUTED_AT = '2026-09-24T11:45:00Z';
/** A recompute that failed after it: 10 minutes before `NOW`. */
export const RECOMPUTE_FAILED_AT = '2026-09-24T11:50:00Z';
/** When a history warning's fill happened: long before `NOW`, and not a relative time. */
export const WARNING_OCCURRED_AT = '2026-03-14T09:30:00Z';

/** The error class name a failed recompute records: never a message. */
export const RECOMPUTE_ERROR = 'UnconvertibleFillError';

/** Zero at the engine's scale, as the backend serialises `0E-18`. */
export const ZERO = '0.000000000000000000';

const AMOUNT_SCALE = 18;
const PRICE_SCALE = 12;
const RETURN_PCT_SCALE = 4;

function fail(message: string): never {
  throw new Error(`Impossible accounting fixture: ${message}`);
}

/**
 * A private `decimal.js` constructor for the checks, so that nothing here depends on - or
 * changes - the global precision `lib/money.ts` sets. Sixty digits hold any product of an
 * 18-place quantity and a 12-place price below 10^20 exactly.
 */
const Exact = Decimal.clone({ precision: 60, rounding: Decimal.ROUND_HALF_EVEN });
type Exact = Decimal;

const PLAIN_DECIMAL = /^-?\d+(?:\.(\d+))?$/;

/** Parses a wire string, refusing one at the wrong scale: the scale is part of the shape. */
function exact(label: string, value: string, scale: number): Exact {
  const match = PLAIN_DECIMAL.exec(value);
  if (match === null) {
    fail(`${label} "${value}" is not a plain decimal string.`);
  }
  const places = match[1]?.length ?? 0;
  if (places !== scale) {
    fail(`${label} "${value}" has ${String(places)} places; the backend sends ${String(scale)}.`);
  }
  return new Exact(value);
}

const VALUE_LIMIT = new Exact('1e20');

/** `quantize(x, 18)`, half to even: the one rounding a product gets in `valuation.py`. */
function toScale(value: Exact, scale: number): Exact {
  return value.toDecimalPlaces(scale, Exact.ROUND_HALF_EVEN);
}

function sameValue(label: string, actual: string | null, expected: Exact, scale: number): void {
  if (actual === null) {
    fail(`${label} is null; it works out to ${expected.toFixed(scale)}.`);
  }
  if (!exact(label, actual, scale).eq(expected)) {
    fail(`${label} is ${actual}; it works out to ${expected.toFixed(scale)}.`);
  }
}

function isPriceReason(reason: MarketValueUnavailable | null): reason is PriceUnavailable {
  return reason !== null && reason !== 'value_out_of_range';
}

/** `unrealized_pnl / cost x 100` at four places, or null when the basis is not positive. */
function returnPct(pnl: Exact, cost: Exact): Exact | null {
  if (cost.lte(0)) {
    return null;
  }
  return toScale(pnl.times(100).dividedBy(cost), RETURN_PCT_SCALE);
}

/** Throws unless `entry` is a position `value_position` can produce. */
export function assertWritablePosition(entry: AccountingPositionResponse): void {
  const asset = entry.asset;
  const quantity = exact(`${asset} quantity`, entry.quantity, AMOUNT_SCALE);
  const unknown = exact(
    `${asset} unknown_basis_quantity`,
    entry.unknown_basis_quantity,
    AMOUNT_SCALE,
  );
  const cost = exact(`${asset} total_invested`, entry.total_invested, AMOUNT_SCALE);
  exact(`${asset} realized_pnl`, entry.realized_pnl, AMOUNT_SCALE);
  exact(`${asset} unmatched_proceeds`, entry.unmatched_proceeds, AMOUNT_SCALE);
  const known = quantity.minus(unknown);

  if (quantity.isNeg() || unknown.isNeg() || known.isNeg()) {
    fail(`${asset}: 0 <= unknown_basis_quantity <= quantity.`);
  }
  if (
    [...entry.flags].sort().join() !== entry.flags.join() ||
    new Set(entry.flags).size !== entry.flags.length
  ) {
    fail(`${asset}: flags are served sorted, once each.`);
  }
  if (entry.flags.includes('unknown_basis') !== !unknown.isZero()) {
    fail(
      `${asset}: unknown_basis describes the pool as it stands - set exactly when units have no known cost.`,
    );
  }

  if (known.isZero()) {
    if (entry.average_cost !== null) {
      fail(`${asset}: there is no average cost without a known-cost unit.`);
    }
    if (!cost.isZero()) {
      fail(`${asset}: nothing of known cost is held, so nothing is invested.`);
    }
  } else if (entry.average_cost !== null) {
    // The average is rounded to 18 places by the engine; the check is that it is the average.
    sameValue(
      `${asset} average_cost`,
      entry.average_cost,
      toScale(cost.dividedBy(known), AMOUNT_SCALE),
      AMOUNT_SCALE,
    );
  }

  if ((entry.market_value === null) !== (entry.market_value_unavailable_reason !== null)) {
    fail(`${asset}: a null market value always has its reason, and a value never has one.`);
  }

  const price =
    entry.price === null ? null : exact(`${asset} price`, entry.price.amount, PRICE_SCALE);

  if (price === null) {
    if (quantity.isZero()) {
      // "`0` when quantity == 0, whatever the price."
      sameValue(`${asset} market_value`, entry.market_value, new Exact(0), AMOUNT_SCALE);
    } else if (!isPriceReason(entry.market_value_unavailable_reason)) {
      fail(`${asset}: with no price, the reason is the price's.`);
    }
    if (known.isZero()) {
      sameValue(`${asset} unrealized_pnl`, entry.unrealized_pnl, new Exact(0), AMOUNT_SCALE);
    } else if (entry.unrealized_pnl !== null) {
      fail(`${asset}: with no price and a known-cost quantity, there is no unrealized P&L.`);
    }
  } else {
    if (isPriceReason(entry.market_value_unavailable_reason)) {
      fail(`${asset}: a priced position has no price reason.`);
    }
    const value = quantity.times(price);
    if (entry.market_value_unavailable_reason === 'value_out_of_range') {
      if (toScale(value, AMOUNT_SCALE).abs().lt(VALUE_LIMIT)) {
        fail(`${asset}: value_out_of_range is for a value of 10^20 or more.`);
      }
      if (entry.unrealized_pnl !== null) {
        fail(`${asset}: an out-of-range value has no unrealized P&L either (R6).`);
      }
    } else {
      sameValue(
        `${asset} market_value`,
        entry.market_value,
        toScale(value, AMOUNT_SCALE),
        AMOUNT_SCALE,
      );
      sameValue(
        `${asset} unrealized_pnl`,
        entry.unrealized_pnl,
        toScale(known.times(price), AMOUNT_SCALE).minus(cost),
        AMOUNT_SCALE,
      );
    }
  }

  const pnl =
    entry.unrealized_pnl === null
      ? null
      : exact(`${asset} unrealized_pnl`, entry.unrealized_pnl, AMOUNT_SCALE);
  const pct = pnl === null ? null : returnPct(pnl, cost);
  if (pct === null) {
    if (entry.unrealized_return_pct !== null) {
      fail(`${asset}: no return without a P&L and a positive basis.`);
    }
  } else {
    sameValue(`${asset} unrealized_return_pct`, entry.unrealized_return_pct, pct, RETURN_PCT_SCALE);
  }
}

/**
 * Why `value_portfolio` leaves `entry` out of the totals, or `null` when it counts it.
 * Checked in declaration order, so a position that is both is `unknown_basis` (R4).
 */
export function exclusionOf(entry: AccountingPositionResponse): ExclusionReason | null {
  if (entry.flags.includes('unknown_basis')) {
    return 'unknown_basis';
  }
  if (entry.market_value === null) {
    return 'unpriced';
  }
  return null;
}

/** Throws unless `response` is one `GET /api/accounting/positions` can serve. */
export function assertWritablePositions(response: PositionsResponse): PositionsResponse {
  const assets = response.positions.map((entry) => entry.asset);
  if (new Set(assets).size !== assets.length || [...assets].sort().join() !== assets.join()) {
    fail('positions are one per asset, read back ordered by asset.');
  }
  response.positions.forEach(assertWritablePosition);

  const recompute = response.last_recompute;
  if (recompute !== null && (recompute.outcome === 'failed') !== (recompute.error !== null)) {
    fail('a failed recompute records its error class, and no other outcome has one.');
  }
  if (response.computed_at === null) {
    if (
      response.positions.length > 0 ||
      response.warnings.length > 0 ||
      response.event_count !== 0
    ) {
      fail('with no snapshot, positions and warnings are empty and event_count is 0.');
    }
    if (recompute !== null && recompute.outcome !== 'failed') {
      fail(`a recompute that was ${recompute.outcome} left a snapshot behind it.`);
    }
  }
  if (response.positions.length > 0 && response.event_count === 0) {
    fail('a position comes from at least one event.');
  }
  exact('unallocated_costs', response.unallocated_costs, AMOUNT_SCALE);

  // The totals, re-derived the way `value_portfolio` derives them.
  const totals = response.totals;
  const expectedExcluded = response.positions.flatMap((entry) => {
    const reason = exclusionOf(entry);
    return reason === null ? [] : [{ asset: entry.asset, reason }];
  });
  if (JSON.stringify(totals.excluded) !== JSON.stringify(expectedExcluded)) {
    fail(
      `totals.excluded is ${JSON.stringify(totals.excluded)}; the positions give ` +
        `${JSON.stringify(expectedExcluded)}.`,
    );
  }
  const counted = response.positions.filter((entry) => exclusionOf(entry) === null);
  const sum = (
    entries: readonly AccountingPositionResponse[],
    field: (entry: AccountingPositionResponse) => string,
  ) => entries.reduce((total, entry) => total.plus(new Exact(field(entry))), new Exact(0));
  const invested = sum(counted, (entry) => entry.total_invested);
  const pnl = sum(
    counted,
    (entry) => entry.unrealized_pnl ?? fail('a counted position has a P&L.'),
  );
  sameValue('totals.total_invested', totals.total_invested, invested, AMOUNT_SCALE);
  sameValue(
    'totals.market_value',
    totals.market_value,
    sum(counted, (entry) => entry.market_value ?? fail('a counted position has a value.')),
    AMOUNT_SCALE,
  );
  sameValue('totals.unrealized_pnl', totals.unrealized_pnl, pnl, AMOUNT_SCALE);
  sameValue(
    'totals.realized_pnl',
    totals.realized_pnl,
    sum(response.positions, (entry) => entry.realized_pnl),
    AMOUNT_SCALE,
  );
  const pct = returnPct(pnl, invested);
  if (pct === null) {
    if (totals.unrealized_return_pct !== null) {
      fail('totals: no return over a basis that is not positive.');
    }
  } else {
    sameValue('totals.unrealized_return_pct', totals.unrealized_return_pct, pct, RETURN_PCT_SCALE);
  }

  return response;
}

/*
 * Builders.
 */

/** A USD price at the stored scale, fresh unless stated. */
export function accountingPrice(overrides: Partial<AccountingPrice> = {}): AccountingPrice {
  return {
    amount: '60000.000000000000',
    source: 'kraken',
    as_of: PRICE_AS_OF,
    stale: false,
    ...overrides,
  };
}

/**
 * BTC, fully comparable, at a gain. Hand-worked:
 *
 * - bought 1.5 at 35000: invested 52500, average 35000;
 * - priced at 60000: market value 1.5 x 60000 = 90000;
 * - unrealized 90000 - 52500 = +37500, a return of 37500 / 52500 x 100 = 71.428571... -> 71.4286;
 * - an earlier sale realized +7500.
 *
 * Not checked on its own: a test that overrides one figure must override the ones derived
 * from it, and {@link positionsResponse} checks the result.
 */
export function position(
  overrides: Partial<AccountingPositionResponse> = {},
): AccountingPositionResponse {
  return {
    asset: 'BTC',
    quantity: '1.500000000000000000',
    unknown_basis_quantity: ZERO,
    average_cost: '35000.000000000000000000',
    total_invested: '52500.000000000000000000',
    realized_pnl: '7500.000000000000000000',
    unmatched_proceeds: ZERO,
    flags: [],
    price: accountingPrice(),
    market_value: '90000.000000000000000000',
    market_value_unavailable_reason: null,
    unrealized_pnl: '37500.000000000000000000',
    unrealized_return_pct: '71.4286',
    ...overrides,
  };
}

/** The BTC position above, exactly. */
export const BTC_GAIN = {
  quantity: '1.500000000000000000',
  averageCost: '35000.000000000000000000',
  invested: '52500.000000000000000000',
  price: '60000.000000000000',
  marketValue: '90000.000000000000000000',
  unrealizedPnl: '37500.000000000000000000',
  returnPct: '71.4286',
  realizedPnl: '7500.000000000000000000',
} as const;

/**
 * ETH: a chain does not price it, so it is `unsupported_pair`, and `history_incomplete`
 * because a sale was larger than the history held. Hand-worked:
 *
 * - 2.718281828459045235 held at an average of 3000: invested 8154.845485377135705000;
 * - that sale realized -250.
 *
 * The quantity uses every one of its 18 places, so a page that parsed it into a double
 * (2.718281828459045) would show in its `<data value>`.
 */
export function ethUnpriced(
  overrides: Partial<AccountingPositionResponse> = {},
): AccountingPositionResponse {
  return position({
    asset: 'ETH',
    quantity: '2.718281828459045235',
    average_cost: '3000.000000000000000000',
    total_invested: '8154.845485377135705000',
    realized_pnl: '-250.000000000000000000',
    flags: ['history_incomplete'],
    price: null,
    market_value: null,
    market_value_unavailable_reason: 'unsupported_pair',
    unrealized_pnl: null,
    unrealized_return_pct: null,
    ...overrides,
  });
}

/**
 * KAS: part of it has no known cost, and its price is stale. Hand-worked:
 *
 * - 1500 held, 500 of them deposited with no known cost, so the known part is 1000;
 * - the 1000 cost 100: average 0.1;
 * - priced at 0.08, two hours old: market value 1500 x 0.08 = 120, over every unit;
 * - unrealized over the known part only: 1000 x 0.08 - 100 = -20, a return of -20 / 100 x 100 = -20.
 */
export function kasUnknownBasis(
  overrides: Partial<AccountingPositionResponse> = {},
): AccountingPositionResponse {
  return position({
    asset: 'KAS',
    quantity: '1500.000000000000000000',
    unknown_basis_quantity: '500.000000000000000000',
    average_cost: '0.100000000000000000',
    total_invested: '100.000000000000000000',
    realized_pnl: ZERO,
    flags: ['unknown_basis'],
    price: accountingPrice({
      amount: '0.080000000000',
      source: 'kaspa',
      as_of: STALE_PRICE_AS_OF,
      stale: true,
    }),
    market_value: '120.000000000000000000',
    unrealized_pnl: '-20.000000000000000000',
    unrealized_return_pct: '-20.0000',
    ...overrides,
  });
}

/**
 * SOL: both unknown-basis and unpriced, and charged a fee it could not value. Excluded once,
 * as `unknown_basis` (R4). Hand-worked:
 *
 * - 10 held, 4 with no known cost, so the known part is 6, which cost 900: average 150;
 * - no price, and a known-cost part held: no value and no P&L.
 */
export function solUnknownAndUnpriced(
  overrides: Partial<AccountingPositionResponse> = {},
): AccountingPositionResponse {
  return position({
    asset: 'SOL',
    quantity: '10.000000000000000000',
    unknown_basis_quantity: '4.000000000000000000',
    average_cost: '150.000000000000000000',
    total_invested: '900.000000000000000000',
    realized_pnl: ZERO,
    flags: ['unattributed_fee', 'unknown_basis'],
    price: null,
    market_value: null,
    market_value_unavailable_reason: 'unsupported_pair',
    unrealized_pnl: null,
    unrealized_return_pct: null,
    ...overrides,
  });
}

/**
 * XRP: fully sold. Nothing held, nothing invested, no average, no price - and a market value
 * and P&L of exactly zero, because a position holding nothing is worth nothing whatever the
 * price. Its sales realized +125.5.
 */
export function xrpClosed(
  overrides: Partial<AccountingPositionResponse> = {},
): AccountingPositionResponse {
  return position({
    asset: 'XRP',
    quantity: ZERO,
    unknown_basis_quantity: ZERO,
    average_cost: null,
    total_invested: ZERO,
    realized_pnl: '125.500000000000000000',
    flags: [],
    price: null,
    market_value: ZERO,
    market_value_unavailable_reason: null,
    unrealized_pnl: ZERO,
    unrealized_return_pct: null,
    ...overrides,
  });
}

/**
 * The totals' figures. `excluded` is left out unless a test states it: {@link positionsResponse}
 * derives it from the positions.
 */
export type TotalsInput = Omit<AccountingTotalsResponse, 'excluded'> & {
  readonly excluded?: ExclusionResponse[];
};

export function totals(overrides: Partial<TotalsInput> = {}): TotalsInput {
  return {
    total_invested: ZERO,
    market_value: ZERO,
    unrealized_pnl: ZERO,
    unrealized_return_pct: null,
    realized_pnl: ZERO,
    ...overrides,
  };
}

export function lastRecompute(
  overrides: Partial<LastRecomputeResponse> = {},
): LastRecomputeResponse {
  return { at: COMPUTED_AT, outcome: 'written', error: null, ...overrides };
}

/** The recompute that failed after the snapshot served was written. */
export function failedRecompute(): LastRecomputeResponse {
  return lastRecompute({ at: RECOMPUTE_FAILED_AT, outcome: 'failed', error: RECOMPUTE_ERROR });
}

export function warning(
  overrides: Partial<AccountingWarningResponse> = {},
): AccountingWarningResponse {
  return {
    kind: 'negative_inventory',
    occurred_at: WARNING_OCCURRED_AT,
    source: 'bitget',
    asset: 'ETH',
    quantity: '0.250000000000000000',
    charged_to: null,
    ...overrides,
  };
}

/**
 * A response, checked. `totals.excluded` is derived from the positions unless stated, the way
 * `finishedRun` derives a run's counters, and a stated one that disagrees is refused.
 */
export type PositionsInput = Partial<Omit<PositionsResponse, 'totals'>> & {
  readonly totals?: TotalsInput;
};

export function positionsResponse(overrides: PositionsInput = {}): PositionsResponse {
  const positions = overrides.positions ?? [];
  const derived = positions.flatMap((entry) => {
    const reason = exclusionOf(entry);
    return reason === null ? [] : [{ asset: entry.asset, reason }];
  });
  const stated = overrides.totals ?? totals();

  return assertWritablePositions({
    method: 'weighted_average',
    quote_currency: 'USD',
    computed_at: COMPUTED_AT,
    event_count: positions.length === 0 ? 0 : 312,
    last_recompute: lastRecompute(),
    unallocated_costs: ZERO,
    warnings: [],
    ...overrides,
    positions,
    totals: { ...stated, excluded: stated.excluded ?? derived },
  });
}

/*
 * Scenarios.
 */

/**
 * The full table: every flag, both exclusion reasons, one position that is both, a stale
 * price and a closed position. Totals, worked by hand over what `value_portfolio` counts:
 *
 * | Asset | Counted? | Invested | Market value | Unrealized | Realized |
 * |---|---|---|---|---|---|
 * | BTC | yes | 52500 | 90000 | +37500 | +7500 |
 * | ETH | no, unpriced | 8154.845485377135705 | - | - | -250 |
 * | KAS | no, unknown basis | 100 | 120 | -20 | 0 |
 * | SOL | no, unknown basis (and unpriced) | 900 | - | - | 0 |
 * | XRP | yes, holds nothing | 0 | 0 | 0 | +125.5 |
 *
 * - invested 52500 + 0 = 52500; market value 90000 + 0 = 90000; unrealized 37500 + 0 = 37500;
 * - return 37500 / 52500 x 100 = 71.4286;
 * - realized 7500 - 250 + 0 + 0 + 125.5 = 7375.5, over every position.
 *
 * `unallocated_costs` is a stablecoin conversion's 0.1 fee.
 */
export function investedPortfolio(overrides: PositionsInput = {}): PositionsResponse {
  return positionsResponse({
    event_count: 312,
    positions: [position(), ethUnpriced(), kasUnknownBasis(), solUnknownAndUnpriced(), xrpClosed()],
    totals: totals({
      total_invested: '52500.000000000000000000',
      market_value: '90000.000000000000000000',
      unrealized_pnl: '37500.000000000000000000',
      unrealized_return_pct: '71.4286',
      realized_pnl: '7375.500000000000000000',
    }),
    unallocated_costs: '0.100000000000000000',
    warnings: [
      warning(),
      warning({
        kind: 'unattributed_fee',
        occurred_at: '2026-05-02T16:20:00Z',
        source: 'bingx',
        asset: 'BGB',
        quantity: '0.002000000000000000',
        charged_to: 'SOL',
      }),
    ],
    ...overrides,
  });
}

/** Exactly the totals of {@link investedPortfolio}. */
export const INVESTED_TOTALS = {
  invested: '52500.000000000000000000',
  marketValue: '90000.000000000000000000',
  unrealizedPnl: '37500.000000000000000000',
  returnPct: '71.4286',
  realizedPnl: '7375.500000000000000000',
  unallocatedCosts: '0.100000000000000000',
} as const;

/**
 * Every held position is excluded, and one closed position is counted, holding nothing. The
 * totals over what is counted are sums over nothing held: zeros the page must not show as
 * figures. Realized P&L still covers every position: -250 + 0 + 125.5 = -124.5.
 */
export function everyHeldPositionExcluded(): PositionsResponse {
  return positionsResponse({
    positions: [ethUnpriced(), kasUnknownBasis(), xrpClosed()],
    totals: totals({ realized_pnl: '-124.500000000000000000' }),
  });
}

/**
 * One comparable position at a loss. Hand-worked: KAS, 1000 bought for 120 (average 0.12),
 * priced at 0.08: value 80, unrealized 80 - 120 = -40, a return of -40 / 120 x 100 =
 * -33.333... -> -33.3333. A sale realized -15.
 */
export function kasLossPortfolio(): PositionsResponse {
  const kas = position({
    asset: 'KAS',
    quantity: '1000.000000000000000000',
    average_cost: '0.120000000000000000',
    total_invested: '120.000000000000000000',
    realized_pnl: '-15.000000000000000000',
    price: accountingPrice({ amount: '0.080000000000', source: 'kaspa' }),
    market_value: '80.000000000000000000',
    unrealized_pnl: '-40.000000000000000000',
    unrealized_return_pct: '-33.3333',
  });
  return positionsResponse({
    positions: [kas],
    totals: totals({
      total_invested: '120.000000000000000000',
      market_value: '80.000000000000000000',
      unrealized_pnl: '-40.000000000000000000',
      unrealized_return_pct: '-33.3333',
      realized_pnl: '-15.000000000000000000',
    }),
  });
}

/**
 * One comparable position exactly at cost: BTC bought 2 at 30000, priced at 30000. Every P&L
 * is zero, and zero is unsigned.
 */
export function breakEvenPortfolio(): PositionsResponse {
  const btc = position({
    quantity: '2.000000000000000000',
    average_cost: '30000.000000000000000000',
    total_invested: '60000.000000000000000000',
    realized_pnl: ZERO,
    price: accountingPrice({ amount: '30000.000000000000' }),
    market_value: '60000.000000000000000000',
    unrealized_pnl: ZERO,
    unrealized_return_pct: '0.0000',
  });
  return positionsResponse({
    positions: [btc],
    totals: totals({
      total_invested: '60000.000000000000000000',
      market_value: '60000.000000000000000000',
      unrealized_pnl: ZERO,
      unrealized_return_pct: '0.0000',
      realized_pnl: ZERO,
    }),
  });
}

/**
 * Gains and losses too small for two places: a tiny gain renders `< +0.01`, a tiny loss
 * `> -0.01`. BTC 0.000001 bought for 0.06 (average 60000), priced at 60000.004: value
 * 0.060000004, unrealized +0.000000004, a return of 0.000000004 / 0.06 x 100 = 0.00000667 ->
 * 0.0000 at four places. Realized -0.003 from an earlier sale.
 */
export function tinyPnlPortfolio(): PositionsResponse {
  const btc = position({
    quantity: '0.000001000000000000',
    average_cost: '60000.000000000000000000',
    total_invested: '0.060000000000000000',
    realized_pnl: '-0.003000000000000000',
    price: accountingPrice({ amount: '60000.004000000000' }),
    market_value: '0.060000004000000000',
    unrealized_pnl: '0.000000004000000000',
    unrealized_return_pct: '0.0000',
  });
  return positionsResponse({
    positions: [btc],
    totals: totals({
      total_invested: '0.060000000000000000',
      market_value: '0.060000004000000000',
      unrealized_pnl: '0.000000004000000000',
      unrealized_return_pct: '0.0000',
      realized_pnl: '-0.003000000000000000',
    }),
  });
}

/*
 * Empty responses, one per row of the empty-state table.
 */

/** A snapshot over no events: startup ran over an empty history. */
export function emptySnapshot(overrides: PositionsInput = {}): PositionsResponse {
  return positionsResponse({ event_count: 0, ...overrides });
}

/**
 * A snapshot over events that made no position: every trade was between stablecoins, which
 * are held at cost. Their fee is the only figure left, in `unallocated_costs`.
 */
export function stablecoinOnlySnapshot(): PositionsResponse {
  return positionsResponse({ event_count: 4, unallocated_costs: '0.200000000000000000' });
}

/** No snapshot, and no recompute attempted since the process started. */
export function noSnapshot(overrides: PositionsInput = {}): PositionsResponse {
  return positionsResponse({
    computed_at: null,
    event_count: 0,
    last_recompute: null,
    ...overrides,
  });
}

/** No snapshot, because the only recompute failed. */
export function failedFirstRecompute(): PositionsResponse {
  return noSnapshot({ last_recompute: failedRecompute() });
}

/** The instant the page reads as "now", re-exported so these tests import one clock. */
export { NOW };
