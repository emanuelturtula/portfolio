import { describe, expect, it } from 'vitest';

import {
  assertWritableFill,
  assertWritableFills,
  at18,
  fill,
  fillsPage,
  fillTotals,
  manyFills,
  marchFills,
  newestFirst,
  ZERO_18,
  type ExchangeFill,
} from './fillFixtures';

/**
 * The controls on the fills fixture guard, and the hand-written totals of the March
 * scenario.
 *
 * The guard is what makes "the fake only serves what the backend can write" a checked
 * statement: each refusal below is a row `NormalizedFill` refuses, and the acceptances
 * beside them show its silence elsewhere means something. The totals are the figures the
 * page tests assert, derived once by hand here, so `fillTotals` is checked against
 * arithmetic nobody ran through it.
 */

const Z = (value: string): string => at18(value);

describe('the fill fixture guard', () => {
  it('writes every amount with exactly 18 places, and refuses a 19th', () => {
    expect(at18('0.5')).toBe('0.500000000000000000');
    expect(at18('-0.3')).toBe('-0.300000000000000000');
    expect(at18('-0')).toBe(ZERO_18);
    expect(at18('0.123456789012345678')).toBe('0.123456789012345678');
    expect(() => at18('0.1234567890123456789')).toThrow('more than 18 places');
    expect(() => at18('1e3')).toThrow('not a plain decimal');
  });

  it('refuses a zero or negative quantity, price or quote quantity', () => {
    const base = { id: 1, executed_at: '2026-03-01T00:00:00Z' };
    expect(() => fill({ ...base, quantity: '0' })).toThrow('quantity is above zero');
    expect(() => fill({ ...base, price: '-1' })).toThrow('price is above zero');
    expect(() => fill({ ...base, quote_quantity: '0' })).toThrow('quote_quantity is above zero');
  });

  it('refuses a fee with no asset, and accepts a zero fee with none', () => {
    const base = { id: 1, executed_at: '2026-03-01T00:00:00Z' };
    expect(() => fill({ ...base, fee_asset: null, fee_amount: '0.1' })).toThrow(
      'null fee_asset is only ever beside a zero fee',
    );
    expect(fill({ ...base, fee_asset: null }).fee_amount).toBe(ZERO_18);
  });

  it('refuses a trade with no leg to account for (spec 020)', () => {
    const base = { id: 1, executed_at: '2026-03-01T00:00:00Z' };
    expect(() => fill({ ...base, base_asset: 'USDT' })).toThrow('different assets');
    // A buy's fee in BTC, the asset received, that eats everything received.
    expect(() => fill({ ...base, quantity: '0.5', fee_amount: '0.5', fee_asset: 'BTC' })).toThrow(
      'leave something received',
    );
    // A buy's rebate in USDT, the asset given, as large as everything given.
    expect(() =>
      fill({ ...base, quantity: '1', price: '10', fee_amount: '-10', fee_asset: 'USDT' }),
    ).toThrow('leave something given');
    // A fee in a third asset constrains neither leg.
    expect(fill({ ...base, fee_amount: '100', fee_asset: 'BNB' }).fee_asset).toBe('BNB');
  });

  it('refuses an instant the API does not write, and a usdt_value it does not derive', () => {
    const row = fill({ id: 1, executed_at: '2026-03-01T00:00:00Z' });
    expect(() => assertWritableFill({ ...row, executed_at: '2026-03-01T00:00:00' })).toThrow(
      'is not how the API writes',
    );
    expect(() => assertWritableFill({ ...row, executed_at: '2026-03-01T00:00:00.5Z' })).toThrow(
      'is not how the API writes',
    );
    expect(() => assertWritableFill({ ...row, usdt_value: null })).toThrow('usdt_value');
    const usdc = fill({ id: 2, executed_at: '2026-03-01T00:00:00Z', quote_asset: 'USDC' });
    expect(usdc.usdt_value).toBeNull();
    expect(() => assertWritableFill({ ...usdc, usdt_value: usdc.quote_quantity })).toThrow(
      'usdt_value',
    );
  });

  it('refuses two rows with one id', () => {
    const [first] = marchFills();
    if (first === undefined) {
      throw new Error('The scenario is empty.');
    }
    expect(() => assertWritableFills([first, first])).toThrow('share an id');
  });

  it('names each venue its own way, and defaults an order id', () => {
    const bitget = fill({ id: 1, executed_at: '2026-03-01T00:00:00Z' });
    const bingx = fill({ id: 2, executed_at: '2026-03-01T00:00:00Z', exchange_key: 'bingx' });
    expect(bitget.symbol).toBe('BTCUSDT');
    expect(bingx.symbol).toBe('BTC-USDT');
    expect(bitget.order_id).toBe('100001');
    expect(bitget.quote_quantity).toBe(Z('30000'));
  });
});

describe('the March scenario totals, worked by hand', () => {
  it('sums per asset, across USDT, per other quote and per fee asset', () => {
    // BTC: bought 0.5 (101), sold 0.75 (102); USDT 30000 spent, 46500 received.
    // ETH: bought 2 (103, USDC) + 1 (104, USDT); only 104 is valued: 3100 spent.
    // SOL: sold 10 (105, quoted in BTC); nothing valued.
    // Fees: BTC 0.0005 + 0.00001, ETH 0.001, USDC -0.3 (a rebate), USDT 12.5.
    expect(fillTotals(marchFills())).toEqual({
      fill_count: 5,
      by_asset: [
        {
          asset: 'BTC',
          fill_count: 2,
          bought: Z('0.5'),
          sold: Z('0.75'),
          net: Z('-0.25'),
          usdt_spent: Z('30000'),
          usdt_received: Z('46500'),
          usdt_net: Z('-16500'),
          usdt_unvalued_fill_count: 0,
        },
        {
          asset: 'ETH',
          fill_count: 2,
          bought: Z('3'),
          sold: ZERO_18,
          net: Z('3'),
          usdt_spent: Z('3100'),
          usdt_received: ZERO_18,
          usdt_net: Z('3100'),
          usdt_unvalued_fill_count: 1,
        },
        {
          asset: 'SOL',
          fill_count: 1,
          bought: ZERO_18,
          sold: Z('10'),
          net: Z('-10'),
          usdt_spent: ZERO_18,
          usdt_received: ZERO_18,
          usdt_net: ZERO_18,
          usdt_unvalued_fill_count: 1,
        },
      ],
      usdt: { spent: Z('33100'), received: Z('46500'), net: Z('-13400') },
      not_valued_in_usdt: {
        fill_count: 2,
        by_quote_asset: [
          {
            quote_asset: 'BTC',
            fill_count: 1,
            spent: ZERO_18,
            received: Z('0.025'),
            net: Z('-0.025'),
          },
          {
            quote_asset: 'USDC',
            fill_count: 1,
            spent: Z('6000'),
            received: ZERO_18,
            net: Z('6000'),
          },
        ],
      },
      fees: [
        { asset: 'BTC', amount: Z('0.00051') },
        { asset: 'ETH', amount: Z('0.001') },
        { asset: 'USDC', amount: Z('-0.3') },
        { asset: 'USDT', amount: Z('12.5') },
      ],
    });
  });

  it('is zero everywhere over no fills', () => {
    expect(fillTotals([])).toEqual({
      fill_count: 0,
      by_asset: [],
      usdt: { spent: ZERO_18, received: ZERO_18, net: ZERO_18 },
      not_valued_in_usdt: { fill_count: 0, by_quote_asset: [] },
      fees: [],
    });
  });

  it('keeps every digit of an 18-place sum longer than 28 significant digits', () => {
    const rows: ExchangeFill[] = [
      fill({
        id: 1,
        executed_at: '2026-03-01T00:00:00Z',
        quantity: '12345678901.123456789012345678',
        price: '1',
        fee_asset: null,
      }),
      fill({
        id: 2,
        executed_at: '2026-03-01T00:00:01Z',
        quantity: '0.000000000000000001',
        price: '1',
        fee_asset: null,
      }),
    ];
    expect(fillTotals(rows).by_asset[0]?.bought).toBe('12345678901.123456789012345679');
  });
});

describe('the fills page', () => {
  it('orders newest first, ties broken by id descending', () => {
    const tie = '2026-03-05T00:00:00Z';
    const rows = [
      fill({ id: 3, executed_at: tie }),
      fill({ id: 9, executed_at: tie }),
      fill({ id: 1, executed_at: '2026-03-06T00:00:00Z' }),
    ];
    expect(newestFirst(rows).map((row) => row.id)).toEqual([1, 9, 3]);
  });

  it('is half-open, pages after filtering, and totals the whole filtered set', () => {
    const rows = manyFills(5, { start: '2026-03-01T00:00:00Z' });
    const everything = { exchanges: [], from: null, to: null, limit: 50, offset: 0 };
    // Minutes 1 to 3: `from` is in, `to` is out.
    const range = {
      ...everything,
      from: Date.parse('2026-03-01T00:01:00Z'),
      to: Date.parse('2026-03-01T00:04:00Z'),
    };

    expect(fillsPage(rows, range).fills.map((row) => row.id)).toEqual([4, 3, 2]);
    const second = fillsPage(rows, { ...range, limit: 2, offset: 2 });
    expect(second.fills.map((row) => row.id)).toEqual([2]);
    expect(second.total_count).toBe(3);
    expect(second.totals.fill_count).toBe(3);
    expect(fillsPage(rows, { ...everything, offset: 99 }).fills).toEqual([]);
    expect(fillsPage(rows, { ...everything, exchanges: ['bingx'] }).total_count).toBe(0);
  });
});
