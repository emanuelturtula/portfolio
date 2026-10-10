/**
 * Fixtures for the change over 24 hours and 7 days (spec 041): `GET /api/portfolio/changes`.
 *
 * Amounts are written at the 18 places the backend serializes a value at, percentages at four,
 * and a change nothing could work out is `null` with its reason, never `"0"`.
 */
import type { Change, ChangeUnavailable, PortfolioChanges } from '@/api/changes';

import { NOW } from './fixtures';

const DAY_MS = 86_400_000;

/** The instant `days` before the fixtures' clock, as the backend writes one. */
function before(days: number): string {
  return new Date(Date.parse(NOW) - days * DAY_MS).toISOString();
}

/** One period's change, worked out. */
export function changeOf(
  period: Change['period'],
  change: string,
  changePct: string | null,
  valueThen = '30000.000000000000000000',
): Change {
  return {
    period,
    since: before(period === '24h' ? 1 : 7),
    value_then: valueThen,
    change,
    change_pct: changePct,
    unavailable: null,
  };
}

/** One period's change, missing for `reason`. */
export function unavailableChange(period: Change['period'], reason: ChangeUnavailable): Change {
  return {
    period,
    since: before(period === '24h' ? 1 : 7),
    value_then: null,
    change: null,
    change_pct: null,
    unavailable: reason,
  };
}

/** The response for these two changes, the value now as given. */
export function portfolioChanges(
  day: Change,
  week: Change,
  value: string | null = '30770.000000000000000000',
): PortfolioChanges {
  return { as_of: NOW, value, changes: [day, week] };
}

/** What the backend answers while the value now is unknown: both changes unavailable. */
export function unknownChanges(): PortfolioChanges {
  return portfolioChanges(
    unavailableChange('24h', 'value_unknown_now'),
    unavailableChange('7d', 'value_unknown_now'),
    null,
  );
}

/** Up 770 over a day (+2.5667 %), down 1230 over a week (-3.8438 %). */
export const MOVED_CHANGES: PortfolioChanges = portfolioChanges(
  changeOf('24h', '770.000000000000000000', '2.5667'),
  changeOf('7d', '-1230.000000000000000000', '-3.8438', '32000.000000000000000000'),
);
