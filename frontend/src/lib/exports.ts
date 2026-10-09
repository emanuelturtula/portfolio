/**
 * Wording for the monthly export reminder (spec 040).
 */

const MONTH_NAMES: Readonly<Record<string, string>> = {
  '01': 'January',
  '02': 'February',
  '03': 'March',
  '04': 'April',
  '05': 'May',
  '06': 'June',
  '07': 'July',
  '08': 'August',
  '09': 'September',
  '10': 'October',
  '11': 'November',
  '12': 'December',
};

/**
 * `2026-09` as "September 2026". A month is a label here, not an instant, so it is read as
 * text: no `Date`, and no time zone that could move it to the month before.
 */
export function formatMonth(month: string): string {
  const [year, number] = month.split('-');
  const name = number === undefined ? undefined : MONTH_NAMES[number];
  return year !== undefined && name !== undefined ? `${name} ${year}` : month;
}

/** "Binance, Bitget, BingX and Nexo". */
export function listExchanges(exchanges: readonly string[]): string {
  return new Intl.ListFormat('en-US', { style: 'long', type: 'conjunction' }).format(exchanges);
}
