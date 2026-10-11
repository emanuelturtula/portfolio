/**
 * The invested figures' words and signs (spec 042), without React.
 *
 * A profit is shown with its sign and a word, "gain" or "loss", as well as in green or red: the
 * colour is never the only thing that tells one from the other. A figure that could not be
 * worked out is a sentence saying why, never `0.00`.
 */
import type { InvestmentUnavailable } from '@/api/operations';
import { directionOf, type Direction } from '@/lib/changes';
import { formatMoney, type Money } from '@/lib/money';
import type { HistoryPoint } from '@/lib/history';

const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const SIGNED = { ...FIAT, signDisplay: 'exceptZero' } as const;
const QUANTITY = { maximumFractionDigits: 8 } as const;
const SIGNED_QUANTITY = { ...QUANTITY, signDisplay: 'exceptZero' } as const;

/** Why a profit, or its percentage, is missing. */
export function investmentUnavailableWords(reason: InvestmentUnavailable): string {
  switch (reason) {
    case 'unvalued_trades':
      return 'Not available: a trade was not priced in USDT, USDC or DAI.';
    case 'value_unknown':
      return 'Not available: the value now is unknown.';
    case 'nothing_invested':
      return 'No percentage: nothing is invested.';
  }
}

/** An amount in USDT at two places: "1,234.56 USDT". */
export function formatUsdt(amount: Money): string {
  return `${formatMoney(amount, FIAT)} USDT`;
}

/** A coin quantity, at up to eight places: "0.0752 BTC". */
export function formatQuantity(quantity: Money, asset: string): string {
  return `${formatMoney(quantity, QUANTITY)} ${asset}`;
}

export interface Profit {
  readonly direction: Direction;
  /** "+1,234.56 USDT gain", "-12.00 USDT loss", or "0.00 USDT, even". */
  readonly text: string;
}

/** A profit with its sign and its word, so it reads the same without colour. */
export function describeProfit(pnl: Money): Profit {
  const direction = directionOf(pnl);
  const word = direction === 'up' ? ' gain' : direction === 'down' ? ' loss' : ', even';
  return { direction, text: `${formatMoney(pnl, SIGNED)} USDT${word}` };
}

/** A percentage, signed, at two places: "+12.50%". */
export function formatSignedPercent(percent: Money): string {
  return `${formatMoney(percent, SIGNED)}%`;
}

/**
 * The wallets against the operations, in words: what the difference means for the owner,
 * not only its number.
 */
export function describeDifference(difference: Money, asset: string): string {
  const direction = directionOf(difference);
  if (direction === 'flat') {
    return 'Matches the operations.';
  }
  const amount = `${formatMoney(difference, SIGNED_QUANTITY)} ${asset}`;
  return direction === 'up'
    ? `${amount}: the wallets hold more than the operations explain.`
    : `${amount}: the wallets hold less than the operations explain.`;
}

/**
 * The invested total as a line over the chart's days: each day carries the last step at or
 * before it, `null` before the first trade and wherever the total is unknown. A gap, never a
 * zero, so a day before anything was uploaded does not read as nothing invested.
 */
export function investedPoints(
  days: readonly string[],
  steps: readonly { readonly day: string; readonly invested: string | null }[],
): HistoryPoint[] {
  return days.map((day) => {
    let value: string | null = null;
    for (const step of steps) {
      if (step.day > day) {
        break;
      }
      value = step.invested;
    }
    return { day, value };
  });
}
