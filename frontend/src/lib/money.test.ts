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

  it('never falls back to exponent notation', () => {
    // decimal.js switches to exponential notation on its own below 1e-7 and
    // above 1e+21. A balance rendered as "1e-18" is a display defect that only
    // shows up on the values this project exists to handle.
    expect(formatMoney(money('0.000000000000000001'))).not.toMatch(/e/i);
    expect(formatMoney(money('123456789012345678901234.5'))).not.toMatch(/e/i);
  });
});
