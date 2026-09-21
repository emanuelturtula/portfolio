import { describe, expect, it } from 'vitest';

import { addMoney, formatMoney, money } from '@/lib/money';

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
