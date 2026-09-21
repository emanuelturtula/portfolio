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
 * Built entirely from `decimal.js` string output and manual string
 * manipulation, never `Number()`, so the module that exists to keep money out
 * of floating point does not reach for one itself.
 *
 * @throws {RangeError} When `minimumFractionDigits` exceeds `maximumFractionDigits`.
 */
export function formatMoney(value: Money, options: FormatMoneyOptions = {}): string {
  const maximumFractionDigits = options.maximumFractionDigits ?? 8;
  const minimumFractionDigits = options.minimumFractionDigits ?? 0;

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
    return decimal.isNegative() ? `> -${threshold}` : `< ${threshold}`;
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

  return negative ? `-${formatted}` : formatted;
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
