import { describe, expect, it } from 'vitest';

import {
  addMoney,
  equalsMoney,
  formatMoney,
  fromBaseUnits,
  isZeroMoney,
  money,
  plainMoney,
  type FormatMoneyOptions,
} from '@/lib/money';

describe('money', () => {
  it('keeps all eighteen decimals of a base-unit amount', () => {
    // Eighteen decimals is the width of a wei-denominated quantity, and with a
    // six-digit integer part it needs twenty-four significant digits. The
    // decimal.js default of twenty cannot hold it, which is why the module sets
    // the precision to forty at load time.
    const raw = '123456.123456789012345678';

    const amount = money(raw);

    // The value object carries the exact string, unrounded and unrenormalised.
    expect(amount).toBe(raw);

    // Arithmetic is where a too-small precision actually bites: decimal.js
    // rounds the *result* of an operation, not the parsed input. The expected
    // value below is written out literally rather than computed, so that it
    // cannot agree with a wrong implementation.
    expect(addMoney(amount, money('0.000000000000000001'))).toBe('123456.123456789012345679');
  });

  it('adds without falling back to exponential notation', () => {
    // Regression. `Decimal.prototype.toString()` switches to exponential below
    // `toExpNeg`, which is -7 by default, so a sum of base-unit amounts came
    // back as "1e-18": a string branded `Money` that `money()` itself rejects
    // and that `<Money>` would put in the DOM as `value="1e-18"`.
    //
    // Every expected value here is written out literally. Computing one with
    // `Decimal` would assert the implementation against itself.
    const sum = addMoney(money('0.000000000000000001'), money('0'));

    expect(sum).toBe('0.000000000000000001');
    expect(sum).not.toMatch(/e/i);
    // The output of one helper has to be legal input to the others, or the
    // value object is not closed over its own operations.
    expect(() => money(sum)).not.toThrow();

    const total = addMoney(money('0.000000000000000001'), money('0.000000000000000002'));
    expect(total).toBe('0.000000000000000003');
    expect(() => money(total)).not.toThrow();
  });

  it('adds two eighteen-decimal amounts exactly', () => {
    const total = addMoney(money('1.000000000000000001'), money('2.000000000000000002'));

    expect(total).toBe('3.000000000000000003');
    expect(() => money(total)).not.toThrow();
  });

  it('adds two amounts without reaching for a float', () => {
    // 0.1 + 0.2 is the canonical IEEE-754 failure: it yields
    // 0.30000000000000004 as a double.
    expect(addMoney(money('0.1'), money('0.2'))).toBe('0.3');
    expect(addMoney(money('-1.5'), money('1.5'))).toBe('0');
  });

  it.each([
    '0',
    '12.34',
    '-1.5',
    '1.123456789012345678',
    '0.000000000000000001',
    '123456789012345678901234.5',
  ])('accepts the decimal string %j', (value) => {
    expect(() => money(value)).not.toThrow();
  });

  it.each([
    '',
    '   ',
    'abc',
    '1.2.3',
    'NaN',
    'Infinity',
    '-Infinity',
    '12,34',
    '1,234.56',
    '$1.00',
    '1 000',
    '--1',
    '.',
    '+',
  ])('rejects a value that is not a decimal number: %j', (value) => {
    expect(() => money(value)).toThrow();
  });

  it('never falls back to exponent notation in the default format', () => {
    // decimal.js switches to exponential notation on its own below 1e-7 and
    // above 1e+21. A balance rendered as "1e-18" is a display defect that only
    // shows up on the values this project exists to handle.
    expect(formatMoney(money('0.000000000000000001'))).not.toMatch(/e/i);
    expect(formatMoney(money('123456789012345678901234.5'))).not.toMatch(/e/i);
  });
});

/**
 * Output assertions for `formatMoney`.
 *
 * Every expected string below is written out by hand. None is computed with
 * `Decimal`, and none is derived from a constant in `money.ts`: a test that
 * builds its expectation the same way the implementation does agrees with the
 * implementation whatever the implementation says, which is how five tests on
 * #3 passed while being worth nothing.
 *
 * This block exists because `money.ts` reported 100% statements, branches and
 * functions with no assertion on what `formatMoney` actually returns. Deleting
 * the sign - so that every negative balance rendered as a positive number -
 * left the whole suite green.
 */
describe('formatMoney', () => {
  it.each([
    ['999', '999'],
    ['1000', '1,000'],
    ['1234567.89', '1,234,567.89'],
    ['1234567890', '1,234,567,890'],
    ['100', '100'],
  ])('groups the thousands of %j as %j', (value, expected) => {
    expect(formatMoney(money(value))).toBe(expected);
  });

  it.each([
    ['-0.5', '-0.5'],
    ['-1234.5', '-1,234.5'],
    ['-1000000', '-1,000,000'],
  ])('keeps the sign of %j as %j', (value, expected) => {
    // A negative balance shown as a positive one is the worst failure this
    // module can have: it is silent, it is plausible, and it is wrong in the
    // direction that matters.
    expect(formatMoney(money(value))).toBe(expected);
  });

  it.each([
    ['1.50', '1.5'],
    ['1.000', '1'],
    ['1.10000000', '1.1'],
    ['0.10', '0.1'],
  ])('trims the trailing zeros of %j to %j', (value, expected) => {
    expect(formatMoney(money(value))).toBe(expected);
  });

  it.each([
    ['1.5', 2, '1.50'],
    ['1', 2, '1.00'],
    ['1234', 2, '1,234.00'],
    ['0', 2, '0.00'],
  ])('pads %j to %i fractional digits as %j', (value, minimumFractionDigits, expected) => {
    expect(formatMoney(money(value), { minimumFractionDigits })).toBe(expected);
  });

  it.each([
    ['1.2345', 2, '1.23'],
    ['1.2345', 8, '1.2345'],
    ['1234.5670001', 3, '1,234.567'],
  ])('limits %j to %i fractional digits as %j', (value, maximumFractionDigits, expected) => {
    expect(formatMoney(money(value), { maximumFractionDigits })).toBe(expected);
  });

  it.each([
    ['1.005', 2, '1.01'],
    ['1.015', 2, '1.02'],
    ['-1.005', 2, '-1.01'],
    ['2.5', 0, '3'],
  ])('rounds %j at %i digits half away from zero, as %j', (value, digits, expected) => {
    // Half-up, decimal.js's default and the one this module ships with. Pinned
    // because a rounding mode nobody wrote down is a rounding mode that changes
    // during a refactor, and the exact half is the only case that reveals it.
    // These are exact decimal halves, not float approximations of them.
    expect(formatMoney(money(value), { maximumFractionDigits: digits })).toBe(expected);
  });

  it.each([
    ['1234.5', 2, 4, '1,234.50'],
    ['-1234.5', 2, 4, '-1,234.50'],
    ['1', 2, 2, '1.00'],
  ])(
    'applies both bounds to %j (min %i, max %i) as %j',
    (value, minimumFractionDigits, maximumFractionDigits, expected) => {
      expect(formatMoney(money(value), { minimumFractionDigits, maximumFractionDigits })).toBe(
        expected,
      );
    },
  );

  it('never renders a negative zero', () => {
    // "-0" is not a number anyone holds. It is the artefact of rounding a tiny
    // negative amount and then keeping the sign anyway.
    expect(formatMoney(money('-0'))).toBe('0');
    expect(formatMoney(money('0'))).toBe('0');
    expect(formatMoney(money('-0.000000000000000001'))).not.toBe('-0');
    expect(formatMoney(money('-0.000000000000000001'))).not.toBe('0');
  });

  it('says an amount is smaller than the precision rather than calling it zero', () => {
    // A balance that is not zero must never be displayed as zero. Someone
    // reading "0" concludes they hold nothing; the truth is that they hold
    // less than the display can show, which is a different statement.
    expect(formatMoney(money('0.000000000000000001'))).toBe('< 0.00000001');
    expect(formatMoney(money('-0.000000000000000001'))).toBe('> -0.00000001');
  });

  it.each([
    ['0.001', 2, '< 0.01'],
    ['-0.001', 2, '> -0.01'],
    ['0.0001', 3, '< 0.001'],
    ['0.4', 0, '< 1'],
  ])('scales that floor to the requested precision: %j at %i -> %j', (value, digits, expected) => {
    // The sentinel is derived from `maximumFractionDigits` alone - never from
    // the value, never from `minimumFractionDigits` - so it reads the same for
    // every amount that rounds away at a given precision.
    expect(formatMoney(money(value), { maximumFractionDigits: digits })).toBe(expected);
  });

  it('renders an exact zero as zero, not as a floor', () => {
    // The floor is for amounts that round away, not for amounts that are zero.
    expect(formatMoney(money('0'), { maximumFractionDigits: 2 })).toBe('0');
  });

  it('refuses a minimum precision greater than the maximum', () => {
    // `Intl.NumberFormat` throws a RangeError on exactly this, and silently
    // under-padding instead would produce a number that claims a precision it
    // was not given.
    expect(() =>
      formatMoney(money('1'), { minimumFractionDigits: 4, maximumFractionDigits: 2 }),
    ).toThrow(RangeError);
  });
});

/**
 * Base units to asset quantity.
 *
 * Every expected string is written out by hand: moving a decimal point is
 * exactly the kind of operation a test must not re-derive with the code it
 * is checking.
 */
describe('fromBaseUnits', () => {
  it.each([
    ['150000000', 8, '1.50000000'],
    ['12345678', 8, '0.12345678'],
    ['1', 8, '0.00000001'],
    ['0', 8, '0.00000000'],
    ['100000000', 8, '1.00000000'],
    ['123', 0, '123'],
    ['5', 2, '0.05'],
  ])('converts %j base units at %i decimals to %j', (units, decimals, expected) => {
    expect(fromBaseUnits(units, decimals)).toBe(expected);
  });

  it('converts a balance past MAX_SAFE_INTEGER without losing a digit', () => {
    // 2870000000000000123 is above Number.MAX_SAFE_INTEGER (9007199254740991).
    // Through a JavaScript number it becomes 2870000000000000000: the last three
    // digits vanish and nothing fails. About the whole Kaspa supply, in sompi.
    expect(fromBaseUnits('2870000000000000123', 8)).toBe('28700000000.00000123');
  });

  it('converts the largest integer the database can store exactly', () => {
    // 2^63 - 1, SQLite's INTEGER ceiling: the widest value the backend can send.
    expect(fromBaseUnits('9223372036854775807', 8)).toBe('92233720368.54775807');
    expect(fromBaseUnits('9223372036854775807', 18)).toBe('9.223372036854775807');
  });

  it('stays exact past the forty significant digits decimal.js is set to', () => {
    // Not reachable from today's backend, whose base units are 64-bit integers,
    // but the function's contract is that moving the decimal point is exact for
    // any integer. A division at precision 40 rounds this to
    // "1111111111111111111111111111111111111111000000000000.00000000", padding
    // the damage with zeros where it looks like data.
    const units = '1'.repeat(60);

    expect(fromBaseUnits(units, 8)).toBe(`${'1'.repeat(52)}.${'1'.repeat(8)}`);
  });

  it.each([
    ['-12000', 8, '-0.00012000'],
    ['-150000000', 8, '-1.50000000'],
    ['-1', 8, '-0.00000001'],
  ])('keeps the sign of a negative pending amount: %j -> %j', (units, decimals, expected) => {
    // `pending` is signed: an outgoing unconfirmed transaction is a negative
    // number of base units, and dropping the sign reports money arriving that
    // is in fact leaving.
    expect(fromBaseUnits(units, decimals)).toBe(expected);
  });

  it('round-trips: the digits of the quantity are the digits of the base units', () => {
    // Removing the decimal point and the leading zeros must give back exactly
    // the integer that went in, for values on both sides of the safe-integer
    // ceiling.
    for (const units of ['1', '12000', '9007199254740993', '2870000000000000123']) {
      const quantity = fromBaseUnits(units, 8);

      expect(quantity.replace('.', '').replace(/^0+(?=\d)/, '')).toBe(units);
      expect(quantity.split('.')[1]).toHaveLength(8);
    }
  });

  it('returns a value the other money helpers accept', () => {
    const quantity = fromBaseUnits('2870000000000000123', 8);

    expect(() => money(quantity)).not.toThrow();
    expect(addMoney(quantity, money('0.00000001'))).toBe('28700000000.00000124');
  });

  it.each(['1.5', '1e3', '', ' 1', '1 ', '+1', '0x10', 'NaN', 'Infinity', '--1', '1,000', '٣'])(
    'refuses %j, which is not an integer string',
    (units) => {
      expect(() => fromBaseUnits(units, 8)).toThrow(TypeError);
    },
  );
});

/**
 * The sign of a profit or a loss (spec 022, "The sign of P&L").
 *
 * The sign is a symbol in the text, so that a gain and a loss read differently without
 * colour. Every expected string is written out by hand.
 */
describe('formatMoney: signDisplay', () => {
  const SIGNED: FormatMoneyOptions = {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
    signDisplay: 'exceptZero',
  };

  it.each([
    ['37500.000000000000000000', '+37,500.00'],
    ['0.010000000000000000', '+0.01'],
    ['71.4286', '+71.43'],
    ['1234567.891', '+1,234,567.89'],
  ])('prefixes a positive amount %j with a plus: %j', (value, expected) => {
    expect(formatMoney(money(value), SIGNED)).toBe(expected);
  });

  it.each([
    ['-20.000000000000000000', '-20.00'],
    ['-33.3333', '-33.33'],
    ['-1234567.891', '-1,234,567.89'],
  ])('keeps the minus of a negative amount %j, and adds nothing: %j', (value, expected) => {
    const formatted = formatMoney(money(value), SIGNED);

    expect(formatted).toBe(expected);
    expect(formatted).not.toMatch(/\+/);
  });

  it.each([
    ['0', '0.00'],
    ['0.000000000000000000', '0.00'],
    ['0.0000', '0.00'],
    ['-0', '0.00'],
    ['-0.000000000000000000', '0.00'],
  ])('leaves zero %j unsigned: %j', (value, expected) => {
    // A break-even position has made nothing. "+0.00" would claim a gain and "-0.00" a loss.
    expect(formatMoney(money(value), SIGNED)).toBe(expected);
  });

  it('leaves a zero unsigned at every precision', () => {
    expect(formatMoney(money('0'), { signDisplay: 'exceptZero' })).toBe('0');
    expect(formatMoney(money('-0'), { signDisplay: 'exceptZero' })).toBe('0');
  });

  it('signs a tiny gain inside the boundary, and keeps the tiny loss as it was', () => {
    // Rule 1 keeps its sign: the exact value is not zero, so neither is what is shown.
    expect(formatMoney(money('0.000000004000000000'), SIGNED)).toBe('< +0.01');
    expect(formatMoney(money('-0.003000000000000000'), SIGNED)).toBe('> -0.01');
    expect(formatMoney(money('0.000000000000000001'), { signDisplay: 'exceptZero' })).toBe(
      '< +0.00000001',
    );
  });

  it('follows the exact value, not the rounded one', () => {
    // 0.005 rounds half away from zero to 0.01, a gain; -0.005 to -0.01. Neither is the
    // boundary, because neither rounds away to nothing.
    expect(formatMoney(money('0.005'), SIGNED)).toBe('+0.01');
    expect(formatMoney(money('-0.005'), SIGNED)).toBe('-0.01');
    // 0.0049 rounds away to nothing at two places: the boundary, still signed.
    expect(formatMoney(money('0.0049'), SIGNED)).toBe('< +0.01');
  });

  it.each([
    ['1234.5'],
    ['-1234.5'],
    ['0'],
    ['-0'],
    ['0.000000000000000001'],
    ['-0.000000000000000001'],
    ['0.005'],
  ])("renders %j under 'auto' exactly as with no option at all", (value) => {
    // 'auto' is today's behaviour, and the default: no existing output changes.
    for (const digits of [{}, { minimumFractionDigits: 2, maximumFractionDigits: 2 }]) {
      expect(formatMoney(money(value), { ...digits, signDisplay: 'auto' })).toBe(
        formatMoney(money(value), digits),
      );
    }
  });

  it("never signs a positive amount under 'auto'", () => {
    expect(formatMoney(money('37500'), { signDisplay: 'auto' })).toBe('37,500');
    expect(formatMoney(money('0.001'), { maximumFractionDigits: 2, signDisplay: 'auto' })).toBe(
      '< 0.01',
    );
    expect(formatMoney(money('-37500'), { signDisplay: 'auto' })).toBe('-37,500');
  });
});

describe('isZeroMoney', () => {
  it.each(['0', '0.00', '-0', '-0.0', '0.000000000000000000', '-0.000000000000000000'])(
    'calls %j zero, however the wire spells it',
    (value) => {
      expect(isZeroMoney(money(value))).toBe(true);
    },
  );

  it.each(['0.000000000000000001', '-0.000000000000000001', '1', '-1', '10.000000000000000000'])(
    'calls %j not zero',
    (value) => {
      // The smallest amount the engine carries is still something held.
      expect(isZeroMoney(money(value))).toBe(false);
    },
  );
});

describe('equalsMoney', () => {
  it.each([
    ['5', '5.000000000000000000'],
    ['0', '-0.000000000000000000'],
    ['10.5', '10.500000000000000000'],
    ['28700000000.00000123', '28700000000.000001230000000000'],
  ])('calls %j and %j the same amount', (a, b) => {
    // The wire spells amounts at their own scale; two spellings of one amount are equal.
    expect(equalsMoney(money(a), money(b))).toBe(true);
    expect(equalsMoney(money(b), money(a))).toBe(true);
  });

  it.each([
    ['10.000000000000000000', '9.999999999999999999'],
    ['0.000000000000000001', '0'],
    ['-1', '1'],
    // Past a double's precision: as numbers these two would compare equal.
    ['9007199254740993', '9007199254740992'],
  ])('tells %j and %j apart', (a, b) => {
    expect(equalsMoney(money(a), money(b))).toBe(false);
  });
});

/**
 * `plainMoney` (spec 027, "What is sent"): the spelling an input holds when the owner edits a
 * stored amount. Every expectation is written out by hand; computing one with `decimal.js`
 * would assert the implementation against itself.
 */
describe('plainMoney', () => {
  /** `[input, the plain spelling]`. */
  const CASES: readonly (readonly [string, string])[] = [
    // The wire's eighteen places, with the zeros nobody typed removed.
    ['1.500000000000000000', '1.5'],
    ['20000.000000000000000000', '20000'],
    ['12000.000000000000000000', '12000'],
    ['0.100000000000000000', '0.1'],
    ['10.010000000000000000', '10.01'],
    // Every one of eighteen places in use: nothing to remove, and nothing rounded. As doubles
    // these are 3.141592653589793 and 1234.5678901234568.
    ['3.141592653589793238', '3.141592653589793238'],
    ['1234.567890123456789012', '1234.567890123456789012'],
    // The integer part is kept whole: only fractional zeros are trailing zeros.
    ['100.000', '100'],
    ['1000000.000000000000000000', '1000000'],
    // No grouping: "1,234,567.89" is not a number a field accepts.
    ['1234567.890000000000000000', '1234567.89'],
    // Every zero is "0".
    ['0.000000000000000000', '0'],
    ['0', '0'],
    ['0.0', '0'],
    // A negative zero never reaches a field.
    ['-0.000000000000000000', '0'],
    ['-0', '0'],
    ['-0.00', '0'],
    // A negative value keeps its sign.
    ['-7375.500000000000000000', '-7375.5'],
    ['-0.000000000000000001', '-0.000000000000000001'],
    // The smallest eighteen-place value: positional, never "1e-18".
    ['0.000000000000000001', '0.000000000000000001'],
    // Twenty digits before the point, the most the engine accepts: never "1.2345678901234567890e+19".
    ['12345678901234567890.000000000000000000', '12345678901234567890'],
    ['10000000000000000000.000000000000000000', '10000000000000000000'],
    // Both at once: thirty-eight significant digits, all of them kept.
    ['99999999999999999999.999999999999999999', '99999999999999999999.999999999999999999'],
    // Already plain: unchanged.
    ['1.5', '1.5'],
    ['42', '42'],
  ];

  it.each(CASES)('spells %j as %j', (input, expected) => {
    expect(plainMoney(money(input))).toBe(expected);
  });

  it.each(CASES)('keeps %j the same amount', (input) => {
    // The spelling changes and the amount does not: nothing is rounded on the way to a field,
    // so what the owner saves untouched is what was stored.
    const plain = plainMoney(money(input));

    expect(equalsMoney(plain, money(input))).toBe(true);
    expect(equalsMoney(money(input), plain)).toBe(true);
  });

  it.each(CASES)(
    'never writes %j with an exponent, a group separator or a padding zero',
    (input) => {
      const plain = plainMoney(money(input));

      // A plain decimal: optional sign, digits, and a fraction that does not end in a zero.
      expect(plain).toMatch(/^-?(?:0|[1-9]\d*)(?:\.\d*[1-9])?$/);
      expect(plain).not.toMatch(/[eE,\s]/);
      // `money()` itself accepts it, so it can go back through every helper here.
      expect(money(plain)).toBe(plain);
    },
  );

  it('is idempotent', () => {
    for (const [input] of CASES) {
      const once = plainMoney(money(input));
      expect(plainMoney(once)).toBe(once);
    }
  });

  it('refuses what is not a plain decimal, as every helper here does', () => {
    // The brand is the only way in, and `money()` is what grants it.
    expect(() => plainMoney(money('1e-18'))).toThrow(TypeError);
    expect(() => plainMoney(money('1,234.5'))).toThrow(TypeError);
    expect(() => plainMoney(money(''))).toThrow(TypeError);
  });
});
