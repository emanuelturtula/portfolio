/**
 * The change widget's words and signs (spec 041), without React.
 *
 * A change is shown with its sign, `+` or `-`, as well as in green or red: the colour is never
 * the only thing that tells a rise from a fall. A change that could not be worked out is a
 * sentence saying why, never `0.00`: a widget that showed no change on the day a price was
 * missing would be believed.
 */
import type { ChangePeriod, ChangeUnavailable } from '@/api/changes';
import { compareMoney, formatMoney, money, type Money } from '@/lib/money';

/** Which way a change went. `flat` is exactly zero. */
export type Direction = 'up' | 'down' | 'flat';

/** Each period's heading, in the order the endpoint serves them. */
export const PERIOD_LABELS: Record<ChangePeriod, string> = {
  '24h': 'Last 24 hours',
  '7d': 'Last 7 days',
};

/** Each period as the end of a sentence: "… 24 hours ago." */
const PERIOD_AGO: Record<ChangePeriod, string> = {
  '24h': '24 hours ago',
  '7d': '7 days ago',
};

/** Why a change is missing, for one period. */
export function unavailableWords(reason: ChangeUnavailable, period: ChangePeriod): string {
  switch (reason) {
    case 'value_unknown_now':
      return 'Not available: the total now is incomplete.';
    case 'no_reading_then':
      return `Not available: no balance is known for ${PERIOD_AGO[period]}.`;
    case 'no_price_then':
      return `Not available: no price was recorded ${PERIOD_AGO[period]}.`;
  }
}

/** Which way `change` went, read exactly from its string. */
export function directionOf(change: Money): Direction {
  const sign = compareMoney(change, money('0'));
  return sign > 0 ? 'up' : sign < 0 ? 'down' : 'flat';
}

const SIGNED = {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
  signDisplay: 'exceptZero',
} as const;

/** A change in USDT, signed: "+1,234.56 USDT", "-12.00 USDT", "0.00 USDT". */
export function formatChange(change: Money): string {
  return `${formatMoney(change, SIGNED)} USDT`;
}

/** A percentage, signed, at two places: "+2.88%". */
export function formatPercent(percent: Money): string {
  return `${formatMoney(percent, SIGNED)}%`;
}
