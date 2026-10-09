/**
 * The currency every figure is shown in.
 *
 * The owner reads the portfolio in USDT (#154). The cached prices are in USD, and a USD price is
 * read as USDT one for one: the dashboard's summary already does so, and the Wallets page asks
 * the balances in USD for the same reason. A depeg would show as a wrong value; the owner
 * accepted that in spec 035.
 */

/** The quote currency the pages ask the backend for. */
export const VALUATION_CURRENCY = 'USD';

/** What a figure valued in {@link VALUATION_CURRENCY} is labelled with. */
export const DISPLAY_CURRENCY = 'USDT';

/**
 * The label for a figure the backend valued in `quoteCurrency`: USD reads as USDT, and anything
 * else keeps its own code, so a response in another currency is never passed off as USDT.
 */
export function currencyLabel(quoteCurrency: string): string {
  return quoteCurrency === VALUATION_CURRENCY ? DISPLAY_CURRENCY : quoteCurrency;
}
