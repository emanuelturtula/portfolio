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
 * most `maximumFractionDigits` fractional digits.
 *
 * This is display formatting only - it may round. The exact, unrounded value
 * is what {@link Money} (the component) also carries in a `<data value>`
 * attribute, which is what makes "no precision loss" a property of the DOM
 * rather than a claim about this function.
 *
 * Built entirely from `decimal.js` string output and manual string
 * manipulation, never `Number()`, so the module that exists to keep money out
 * of floating point does not reach for one itself.
 */
export function formatMoney(value: Money, options: FormatMoneyOptions = {}): string {
  const maximumFractionDigits = options.maximumFractionDigits ?? 8;
  const minimumFractionDigits = options.minimumFractionDigits ?? 0;

  const fixed = new Decimal(value).toFixed(maximumFractionDigits);
  const negative = fixed.startsWith('-');
  const unsigned = negative ? fixed.slice(1) : fixed;
  const [wholePart = '0', fractionPart = ''] = unsigned.split('.');

  const groupedWhole = wholePart.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const fraction = trimTrailingZeros(fractionPart, minimumFractionDigits);
  const formatted = fraction.length > 0 ? `${groupedWhole}.${fraction}` : groupedWhole;

  return negative ? `-${formatted}` : formatted;
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
