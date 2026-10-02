import { describe, expect, it } from 'vitest';

import {
  adjustment,
  ADJUSTMENT_CREATED_AT,
  adjustmentList,
  asStoredAmount,
  asStoredInstant,
  BTC_OPENING,
  ethPrecise,
  firstTrade,
  firstTrades,
  inReplayOrder,
  instantKey,
  kasUnknownCost,
  threeAdjustments,
  threeFirstTrades,
} from './adjustmentFixtures';

/**
 * The controls on the adjustment fixture guard.
 *
 * Every page test of spec 027 asserts on what these builders return, so each rule below is a
 * claim about what `GET /api/accounting/adjustments` can serve. A guard that accepted
 * everything would make every one of those tests pass against a response no backend sends,
 * and say nothing; so each refusal is shown to fire, beside the writable value next to it.
 */
describe('the adjustment fixture guard', () => {
  it('accepts the scenarios the page tests use', () => {
    expect(threeAdjustments().map((entry) => entry.asset)).toEqual(['BTC', 'KAS', 'ETH']);
    expect(adjustmentList(threeAdjustments()).adjustments).toHaveLength(3);
    expect(kasUnknownCost().unit_cost).toBeNull();
    expect(ethPrecise().quantity).toBe('3.141592653589793238');
  });

  it.each([
    ['1.5', 'has 1 places; the backend sends 18'],
    ['1', 'has 0 places; the backend sends 18'],
    ['1.5000000000000000000', 'has 19 places; the backend sends 18'],
    ['1e0', 'is not a plain decimal string'],
    ['1,5', 'is not a plain decimal string'],
  ])('refuses a quantity of %j: an amount is served at eighteen places', (quantity, reason) => {
    expect(() => adjustment({ quantity })).toThrow(reason);
  });

  it('refuses a unit cost that is not at eighteen places, and accepts null', () => {
    expect(() => adjustment({ unit_cost: '20000' })).toThrow('has 0 places');
    expect(adjustment({ unit_cost: null }).unit_cost).toBeNull();
  });

  it.each(['0.000000000000000000', '-1.500000000000000000'])(
    'refuses a quantity of %j: it is greater than zero',
    (quantity) => {
      expect(() => adjustment({ quantity })).toThrow('quantity is greater than zero');
    },
  );

  it('refuses a negative unit cost and a signed zero, and accepts a cost of nothing', () => {
    expect(() => adjustment({ unit_cost: '-0.010000000000000000' })).toThrow(
      'unit_cost is zero or more, or null for unknown',
    );
    expect(() => adjustment({ unit_cost: '-0.000000000000000000' })).toThrow(
      'a zero is stored and served unsigned',
    );
    // Zero is a known cost of nothing, which is not an unknown cost.
    expect(adjustment({ unit_cost: '0.000000000000000000' }).unit_cost).toBe(
      '0.000000000000000000',
    );
  });

  it('refuses an amount, or a total cost, with more than twenty digits before the point', () => {
    expect(() => adjustment({ quantity: '100000000000000000000.000000000000000000' })).toThrow(
      'quantity has at most 20 digits',
    );
    expect(() =>
      adjustment({
        quantity: '31415926535.000000000000000000',
        unit_cost: '27182818284.000000000000000000',
      }),
    ).toThrow('unit_cost times quantity');
    // The largest quantity there is, at a cost nobody knows: no total to overflow.
    expect(
      adjustment({ quantity: '99999999999999999999.999999999999999999', unit_cost: null }).quantity,
    ).toBe('99999999999999999999.999999999999999999');
  });

  it.each(['btc', 'Btc', 'BTC ', '', 'BTC-USD', 'A'.repeat(21)])(
    'refuses the asset %j: the symbol is 1 to 20 upper-case letters or digits',
    (asset) => {
      expect(() => adjustment({ asset })).toThrow('is not 1 to 20 upper-case letters or digits');
    },
  );

  it.each(['USDC', 'USDT'])('refuses the cash asset %s', (asset) => {
    expect(() => adjustment({ asset })).toThrow(`an adjustment of the cash asset ${asset}`);
  });

  it.each([
    ['2025-02-28T23:59:37.123Z', "three fractional digits are JavaScript's spelling"],
    ['2025-02-28T23:59:37.000000Z', 'no microseconds are serialised as none'],
    ['2025-02-28T23:59:37+00:00', 'Pydantic writes UTC as Z'],
    ['2025-02-28T23:59:37', 'a naive datetime is refused at entry'],
    ['2025-02-28 23:59:37Z', 'a space is not the separator'],
    ['2025-02-28', 'a day is not an instant'],
  ])('refuses an occurred_at of %j: %s', (occurredAt) => {
    expect(() => adjustment({ occurred_at: occurredAt })).toThrow(
      /is not a UTC instant|no microseconds/,
    );
  });

  it('refuses an instant no calendar holds', () => {
    expect(() => adjustment({ occurred_at: '2025-13-01T00:00:00Z' })).toThrow(
      'names no real instant',
    );
  });

  it('refuses an occurred_at later than the last write, which the future rule forbade', () => {
    expect(() => adjustment({ occurred_at: '2026-09-20T09:00:01Z' })).toThrow(
      'occurred_at was not later than now when it was last written',
    );
    // One written at the instant it was acquired is not in the future.
    expect(adjustment({ occurred_at: ADJUSTMENT_CREATED_AT }).occurred_at).toBe(
      ADJUSTMENT_CREATED_AT,
    );
  });

  it('refuses an update before the creation', () => {
    expect(() => adjustment({ updated_at: '2026-09-20T08:59:59Z' })).toThrow(
      'it was last written at or after it was created',
    );
  });

  it.each(['', '   ', '\n\t'])('refuses the blank note %j', (note) => {
    expect(() => adjustment({ note })).toThrow('a note is required, and not blank');
  });

  it('refuses a note over 500 characters, and accepts one of exactly 500', () => {
    expect(() => adjustment({ note: 'n'.repeat(501) })).toThrow('at most 500 characters');
    expect(adjustment({ note: 'n'.repeat(500) }).note).toHaveLength(500);
  });

  it.each([0, -1, 1.5])('refuses the id %j', (id) => {
    expect(() => adjustment({ id })).toThrow('an id is a positive integer');
  });

  it('refuses a list that is not in replay order, or that repeats an id', () => {
    const [btc, kas, eth] = threeAdjustments();
    if (btc === undefined || kas === undefined || eth === undefined) {
      throw new Error('threeAdjustments() gives three.');
    }

    expect(() => adjustmentList([kas, btc, eth])).toThrow(
      'the list is served by occurred_at and then id: 1, 2, 3, not 2, 1, 3',
    );
    expect(() => adjustmentList([btc, btc])).toThrow('an id names one adjustment');
  });

  it('orders two adjustments at one instant by id, and a fraction after its whole second', () => {
    // `...:37Z` is before `...:37.123456Z`, which their spellings, compared as text, deny:
    // "." sorts before "Z".
    const whole = adjustment({ id: 7, occurred_at: '2025-02-28T23:59:37Z' });
    const fraction = adjustment({ id: 3, occurred_at: '2025-02-28T23:59:37.123456Z' });
    const tie = adjustment({ id: 5, occurred_at: '2025-02-28T23:59:37Z' });

    expect(inReplayOrder([fraction, whole, tie]).map((entry) => entry.id)).toEqual([5, 7, 3]);
    expect(adjustmentList([tie, whole, fraction]).adjustments).toHaveLength(3);
    expect(
      instantKey('a', '2025-02-28T23:59:37Z') < instantKey('b', '2025-02-28T23:59:37.123456Z'),
    ).toBe(true);
  });
});

describe('the stored form of what a request carries', () => {
  it.each([
    ['1.5', '1.500000000000000000'],
    ['20000', '20000.000000000000000000'],
    ['0', '0.000000000000000000'],
    ['-0', '0.000000000000000000'],
    ['3.141592653589793238', '3.141592653589793238'],
    ['.5', '0.500000000000000000'],
  ])('stores the amount %j as %j', (sent, stored) => {
    expect(asStoredAmount(sent)).toBe(stored);
  });

  it.each([
    // What `toISOString()` sends: milliseconds, which become microseconds or nothing.
    ['2025-03-01T10:00:00.000Z', '2025-03-01T10:00:00Z'],
    ['2025-03-01T10:00:37.120Z', '2025-03-01T10:00:37.120000Z'],
    // What an untouched edit sends: the stored instant itself, which is stored again as it is.
    ['2025-02-28T23:59:37.123456Z', '2025-02-28T23:59:37.123456Z'],
    ['2025-06-01T12:00:05Z', '2025-06-01T12:00:05Z'],
    // An offset is converted to UTC, as `view_of` serves it.
    ['2025-06-01T14:00:00+02:00', '2025-06-01T12:00:00Z'],
    ['2025-06-01T00:30:00.5-03:00', '2025-06-01T03:30:00.500000Z'],
  ])('stores the instant %j as %j', (sent, stored) => {
    expect(asStoredInstant(sent)).toBe(stored);
  });

  it('reads a datetime with no offset as naive, and refuses what is not ISO 8601', () => {
    expect(asStoredInstant('2025-06-01T12:34:56')).toBe('naive');
    expect(asStoredInstant('1767225600')).toBeNull();
    expect(asStoredInstant('yesterday')).toBeNull();
    expect(asStoredInstant('')).toBeNull();
  });
});

describe('the first-trades fixture guard', () => {
  it('accepts an owner with no fills, and the three assets the page tests use', () => {
    expect(firstTrades().assets).toEqual([]);
    expect(threeFirstTrades().assets.map((entry) => entry.asset)).toEqual(['BTC', 'ETH', 'KAS']);
  });

  it('refuses a list that is not sorted by asset, or that names one twice', () => {
    expect(() => firstTrades([firstTrade('ETH'), firstTrade('BTC')])).toThrow('sorted by asset');
    expect(() => firstTrades([firstTrade('BTC'), firstTrade('BTC')])).toThrow(
      'one entry per asset',
    );
  });

  it('sorts in code-point order, where a digit and an upper-case letter come first', () => {
    expect(
      firstTrades([firstTrade('1INCH'), firstTrade('BTC'), firstTrade('btc')]).assets,
    ).toHaveLength(3);
    expect(() => firstTrades([firstTrade('btc'), firstTrade('BTC')])).toThrow('sorted by asset');
  });

  it.each(['USDC', 'USDT'])('refuses the cash asset %s, which the endpoint leaves out', (asset) => {
    expect(() => firstTrades([firstTrade(asset)])).toThrow(`leaves the cash asset ${asset} out`);
  });

  it('refuses an instant that is not as the backend serialises one', () => {
    expect(() => firstTrades([firstTrade('BTC', '2025-03-01T10:00:37.250Z')])).toThrow(
      'is not a UTC instant as the backend serialises one',
    );
    expect(() => firstTrades([firstTrade('', '2025-03-01T10:00:37Z')])).toThrow('has a name');
  });

  it("keeps the spec example as BTC's first trade", () => {
    expect(threeFirstTrades().assets[0]).toEqual({
      asset: BTC_OPENING.asset,
      first_trade_at: '2025-03-01T10:00:37Z',
    });
  });
});
