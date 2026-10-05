/**
 * Money value object backed by `decimal.js`.
 *
 * The backend serialises every monetary amount as a plain decimal JSON
 * *string* - never a JSON number - because IEEE-754 doubles cannot represent
 * a value like an 18-decimal on-chain amount exactly. This module is the one
 * place in the frontend allowed to reach for `decimal.js`; everywhere else
 * imports the `Money` type and these helpers instead of parsing the string
 * itself. `eslint.config.js` enforces that by banning `parseFloat`,
 * `parseInt`, `Number()` and unary `+` across `src/lib/**`, `src/components/**`,
 * `src/pages/**` and `src/features/**`.
 */
import Decimal from 'decimal.js';

/**
 * A precision of 40 significant digits is set exactly once, here, at module
 * load. The default of 20 cannot round-trip an 18-decimal base-unit amount
 * without loss; 40 leaves headroom for the arithmetic `addMoney` performs.
 *
 * `Decimal.set` is global process state - a second call anywhere else in the
 * codebase would silently change every other computation's precision - which
 * is why this is the only file allowed to call it.
 */
Decimal.set({ precision: 40 });

/** A validated decimal amount, over the wire and everywhere in the frontend. */
export type Money = string & { readonly __brand: 'Money' };

/** A plain decimal number: optional sign, digits, optional fractional part. */
const DECIMAL_PATTERN = /^-?\d+(?:\.\d+)?$/;

/**
 * Validates that `value` is a plain decimal string and brands it as {@link Money}.
 *
 * Deliberately rejects anything `decimal.js` would still accept but the
 * backend never produces: scientific notation, leading `+`, `Infinity`,
 * `NaN`. A value this application treats as money always came from the
 * backend as a plain decimal string, so a value that does not look like one
 * is a bug, not a formatting variant to accommodate.
 *
 * @throws {TypeError} When `value` is not a plain decimal string.
 */
export function money(value: string): Money {
  if (!DECIMAL_PATTERN.test(value)) {
    throw new TypeError(`"${value}" is not a valid decimal amount.`);
  }

  return value as Money;
}

export interface FormatMoneyOptions {
  /** Maximum fractional digits to show. Defaults to 8. */
  readonly maximumFractionDigits?: number;
  /** Minimum fractional digits to show, padding with zeros. Defaults to 0. */
  readonly minimumFractionDigits?: number;
  /**
   * When a sign is shown, named after `Intl.NumberFormat`'s option of the same name.
   *
   * - `'auto'`, the default: only a negative amount carries a sign.
   * - `'exceptZero'`: a positive amount is prefixed with `+`, a negative one keeps its `-`,
   *   and zero stays unsigned. For profit and loss, where the sign is the only thing that
   *   tells a gain from a loss once colour is not relied on.
   *
   * The sign follows the exact value, never the rounded one, so a gain too small to show
   * renders as the boundary `< +0.01` and never as `+0.00`.
   */
  readonly signDisplay?: 'auto' | 'exceptZero';
}

/**
 * Renders a {@link Money} value for display: grouped thousands, rounded to at
 * most `maximumFractionDigits` fractional digits, padded to at least
 * `minimumFractionDigits`.
 *
 * This is display formatting only - it may round. The exact, unrounded value
 * is what {@link Money} (the component) also carries in a `<data value>`
 * attribute, which is what makes "no precision loss" a property of the DOM
 * rather than a claim about this function. Rounding for display is still
 * bound by two rules a portfolio balance cannot bend:
 *
 * 1. A non-zero amount that rounds away to nothing at the requested precision
 *    is never shown as `0` - a wei-scale balance and an empty one are a
 *    different answer to "do I have anything here", and collapsing them is
 *    exactly the fabricated-zero failure this module exists to prevent. Such
 *    a value renders as a boundary instead, e.g. `< 0.00000001`.
 * 2. A genuine zero is never shown as `-0`. `decimal.js` itself normalises the
 *    sign away in `toFixed` once the rounded magnitude is exactly zero - it
 *    only keeps a sign on a zero-magnitude result when rounding *reached*
 *    zero from a non-zero value, which is precisely the case rule 1 already
 *    intercepts above. This function does not re-guard it; the guarantee
 *    lives in the test asserting `formatMoney(money('-0'))` renders `'0'` -
 *    if a future `decimal.js` ever stops normalising it, that test is what
 *    fails.
 *
 * `minimumFractionDigits` must not exceed `maximumFractionDigits` - the same
 * invariant `Intl.NumberFormat` enforces - because the alternative is padding
 * that silently never happens.
 *
 * `signDisplay: 'exceptZero'` adds a `+` to a positive amount, including the boundary of
 * rule 1 (`< +0.01`); a negative amount and a zero render exactly as they do under `'auto'`.
 *
 * Built entirely from `decimal.js` string output and manual string
 * manipulation, never `Number()`, so the module that exists to keep money out
 * of floating point does not reach for one itself.
 *
 * @throws {RangeError} When `minimumFractionDigits` exceeds `maximumFractionDigits`.
 */
export function formatMoney(value: Money, options: FormatMoneyOptions = {}): string {
  const maximumFractionDigits = options.maximumFractionDigits ?? 8;
  const minimumFractionDigits = options.minimumFractionDigits ?? 0;
  const plus = options.signDisplay === 'exceptZero' ? '+' : '';

  if (minimumFractionDigits > maximumFractionDigits) {
    throw new RangeError(
      `minimumFractionDigits (${String(minimumFractionDigits)}) must not be greater than ` +
        `maximumFractionDigits (${String(maximumFractionDigits)}).`,
    );
  }

  const decimal = new Decimal(value);
  const rounded = decimal.toDecimalPlaces(maximumFractionDigits);

  if (rounded.isZero() && !decimal.isZero()) {
    const threshold = smallestUnit(maximumFractionDigits);
    return decimal.isNegative() ? `> -${threshold}` : `< ${plus}${threshold}`;
  }

  const fixed = rounded.toFixed(maximumFractionDigits);
  // `fixed` never starts with `-` when `rounded` is exactly zero - see rule 2
  // above - so reading the sign straight off `fixed` already excludes `-0`
  // without this function re-checking `rounded.isZero()` itself.
  const negative = fixed.startsWith('-');
  const unsigned = negative ? fixed.slice(1) : fixed;
  const [wholePart = '0', fractionPart = ''] = unsigned.split('.');

  const groupedWhole = wholePart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = trimTrailingZeros(fractionPart, minimumFractionDigits);
  const formatted = fraction.length > 0 ? `${groupedWhole}.${fraction}` : groupedWhole;

  if (negative) {
    return `-${formatted}`;
  }

  // Reaching here with a non-zero `decimal` means `rounded` is non-zero too - rule 1 above
  // has already returned for every value that rounds away - so `!decimal.isZero()` is
  // also "the digits shown are not all zero", and `+0.00` cannot be produced.
  return decimal.isZero() ? formatted : `${plus}${formatted}`;
}

/** The smallest positive amount representable at `maximumFractionDigits`, e.g. `0.01` for 2. */
function smallestUnit(maximumFractionDigits: number): string {
  if (maximumFractionDigits === 0) {
    return '1';
  }

  return `0.${'0'.repeat(maximumFractionDigits - 1)}1`;
}

/**
 * Removes trailing zeros from a fractional part, keeping at least
 * `minimumDigits` characters. `fraction` is always `toFixed`'s zero-padded
 * output, so the characters up to `minimumDigits` are already there - no
 * padding back is needed, only deciding where to stop trimming.
 */
function trimTrailingZeros(fraction: string, minimumDigits: number): string {
  let end = fraction.length;

  while (end > minimumDigits && fraction[end - 1] === '0') {
    end -= 1;
  }

  return fraction.slice(0, end);
}

/** A plain integer: optional sign, digits only - no fractional part, no exponent. */
const INTEGER_PATTERN = /^-?\d+$/;

/**
 * Converts an integer count of base units (satoshis, sompi) into a decimal quantity of the
 * asset, given how many decimals it uses.
 *
 * Implemented as a string shift, deliberately not `Decimal#dividedBy`: that method rounds
 * its result to the module's 40-digit precision, so a base-unit string longer than 40
 * digits would come back silently rounded - unreachable from the backend today, where base
 * units are a SQLite `INTEGER` capped at 19 digits, but this module's whole reason to exist
 * is not depending on a caller staying inside a limit it does not enforce. Moving the
 * decimal point across the string is exact at any length and needs no `Decimal` at all.
 *
 * `units` may carry a leading `-`, because `pending` is a signed amount.
 *
 * @throws {TypeError} When `units` is not a plain integer string.
 */
export function fromBaseUnits(units: string, decimals: number): Money {
  if (!INTEGER_PATTERN.test(units)) {
    throw new TypeError(`"${units}" is not a valid base-unit integer.`);
  }

  const negative = units.startsWith('-');
  const digits = negative ? units.slice(1) : units;
  // Padded so there is always at least one digit before the split point, even when `units`
  // has fewer digits than `decimals` - e.g. "12" at 5 decimals becomes "0.00012".
  const padded = digits.padStart(decimals + 1, '0');
  const splitAt = padded.length - decimals;
  const whole = padded.slice(0, splitAt);
  const fraction = padded.slice(splitAt);
  const unsigned = fraction.length > 0 ? `${whole}.${fraction}` : whole;

  return money(negative ? `-${unsigned}` : unsigned);
}

/** `money(value)`, or `null` for a figure the backend sent as `null`. */
export function moneyOrNull(value: string | null): Money | null {
  return value === null ? null : money(value);
}

/** Adds two {@link Money} values with no precision loss and returns another. */
export function addMoney(a: Money, b: Money): Money {
  // `toString` (and `toJSON`/`valueOf`) switch to exponential notation once the
  // exponent passes decimal.js's `toExpNeg`/`toExpPos` thresholds (-7/21 by
  // default) - which an 18-decimal on-chain amount does routinely, not as an
  // edge case. `toFixed()` with no argument is specified to never use
  // exponential notation and, unlike `toFixed(n)`, does not round: it is the
  // one method here that is both exact and always plain.
  //
  // The result is re-validated through `money()` rather than cast: a brand
  // that can be applied to an unvalidated string is a brand that will
  // eventually be wrong, and the cost of one more regex test is negligible
  // next to what a silently-mistagged value would cost downstream.
  return money(new Decimal(a).plus(new Decimal(b)).toFixed());
}

/**
 * The exact plain spelling of `value`: no grouping, no exponent, no rounding, and no trailing
 * zeros. For a value an input can hold, such as an amount the owner is about to edit, where
 * the wire's `"1.500000000000000000"` would show eighteen places nobody typed and
 * {@link formatMoney}'s `"1,234.5"` is not a number a field accepts.
 *
 * The result is the same amount: nothing is rounded, so a value of 18 places keeps all 18.
 * The edges:
 *
 * - every zero is `"0"`, whatever its spelling: `"0.000000000000000000"` and `"-0.00"` too, so
 *   a negative zero never reaches a field;
 * - a very small or very large value stays in positional notation (`"0.000000000000000001"`,
 *   not `"1e-18"`), because `toFixed()` with no argument is the one `decimal.js` output that
 *   never uses an exponent - the reason {@link addMoney} uses it as well;
 * - the integer part is kept whole: `"100.000"` is `"100"`, never `"1"`.
 */
export function plainMoney(value: Money): Money {
  return money(new Decimal(value).toFixed());
}

/**
 * Whether `value` is exactly zero, however the wire spells it: `"0"`, `"0.00"`, `"-0"` and
 * `"0.000000000000000000"` are all zero. Comparing the strings would call the last one
 * non-zero, which is how a fully sold position ends up listed as held.
 */
export function isZeroMoney(value: Money): boolean {
  return new Decimal(value).isZero();
}

/**
 * Whether `a` and `b` are the same amount, however each is spelled: `"5"` equals
 * `"5.000000000000000000"`, which a comparison of the strings would call different.
 */
export function equalsMoney(a: Money, b: Money): boolean {
  return new Decimal(a).equals(new Decimal(b));
}

/** Whether `value` is below zero. A negative zero is not. */
export function isNegativeMoney(value: Money): boolean {
  const decimal = new Decimal(value);
  return decimal.isNegative() && !decimal.isZero();
}

/** Orders two amounts for `Array.prototype.sort`: negative when `a` is the smaller. */
export function compareMoney(a: Money, b: Money): number {
  return new Decimal(a).comparedTo(new Decimal(b));
}

/** Which way a signed amount points. */
export type Tone = 'gain' | 'loss' | 'flat';

/**
 * The direction of a profit, a loss or a return, for the colour drawn under its sign. Never the
 * only channel: the figure it colours always carries its `+` or `-`.
 */
export function toneOf(value: Money): Tone {
  if (isZeroMoney(value)) {
    return 'flat';
  }
  return isNegativeMoney(value) ? 'loss' : 'gain';
}

/** The largest of `values`, or zero when there are none. Exact, like every comparison here. */
export function maxMoney(values: readonly Money[]): Money {
  return values.reduce<Money>(
    (largest, value) => (new Decimal(value).greaterThan(new Decimal(largest)) ? value : largest),
    money('0'),
  );
}

/**
 * How long a bar is on a chart whose longest bar is `max`: `value` as a percentage of it, as a
 * CSS length such as `"42.5%"`. Worked out in decimal and handed to the stylesheet as a string,
 * so no figure passes through a float on its way to the screen.
 *
 * A bar only grows to the right, from zero: a value at or below zero, or a scale with nothing
 * on it, has no length at all (`"0%"`), and nothing is ever longer than the whole track.
 */
export function barLength(value: Money, max: Money): string {
  const top = new Decimal(max);
  const length = new Decimal(value);
  if (top.lessThanOrEqualTo(0) || length.lessThanOrEqualTo(0)) {
    return '0%';
  }
  return `${Decimal.min(length.dividedBy(top), 1).times(100).toDecimalPlaces(2).toFixed()}%`;
}

/**
 * A {@link Money} as a JavaScript number, for one purpose only: **placing a mark on a chart**.
 *
 * A chart library positions an arc or a point with floating-point geometry, and no amount of
 * care upstream changes that. What this function bounds is where the loss can land: in the
 * pixels of a mark, never in a figure a person reads. Every label, legend and tooltip beside
 * the chart renders the original string through {@link formatMoney}, so the number this
 * returns is never shown.
 *
 * It is the one place in `src/` allowed to call `toNumber()`; `eslint.config.js` refuses it
 * everywhere else, beside `parseFloat` and `Number()`.
 */
export function toChartNumber(value: Money): number {
  return new Decimal(value).toNumber();
}
