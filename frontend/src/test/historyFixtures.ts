/**
 * Fixtures for the value history (spec 037): `GET /api/portfolio/history` and
 * `GET /api/wallets/{wallet_id}/value-history`.
 *
 * Every history ends today and has one point per day, oldest first, as the backend serves it.
 * Values are written at the 18 places the backend serializes a value at, and a day nothing
 * could value is `null`, never `"0"`.
 */
import { screen, waitFor, within } from '@testing-library/react';
import { expect } from 'vitest';

import type { HistoryRange, PortfolioHistory, WalletValueHistory } from '@/api/history';

/** Today in UTC at the fixtures' clock (`NOW` in `fixtures.ts`): every history ends on it. */
export const HISTORY_TODAY = '2026-09-24';

const DAY_MS = 86_400_000;

/** How many days a range has. `all` starts at the first reading, so its length is a choice. */
export const RANGE_DAYS: Record<HistoryRange, number> = {
  '30d': 30,
  '90d': 90,
  '1y': 365,
  all: 3,
};

/** `count` consecutive UTC days ending on `end`, oldest first. */
export function daysEnding(count: number, end: string = HISTORY_TODAY): string[] {
  const endMs = Date.parse(`${end}T00:00:00Z`);
  return Array.from({ length: count }, (_, index) =>
    new Date(endMs - (count - 1 - index) * DAY_MS).toISOString().slice(0, 10),
  );
}

/** A portfolio history with one point per value, ending today. */
export function portfolioHistory(
  values: readonly (string | null)[],
  range: HistoryRange = '90d',
): PortfolioHistory {
  const days = daysEnding(values.length);
  return { range, points: days.map((day, index) => ({ day, value: values[index] ?? null })) };
}

/** What the backend answers before anything could be valued: every day of the range `null`. */
export function unvaluedPortfolioHistory(range: HistoryRange): PortfolioHistory {
  return portfolioHistory(Array<null>(RANGE_DAYS[range]).fill(null), range);
}

/** One wallet's day: its quantity and its value, either of them `null`. */
export type WalletDay = readonly [quantity: string | null, value: string | null];

/** One wallet's history with one point per day given, ending today. */
export function walletValueHistory(
  walletId: number,
  asset: string,
  days: readonly WalletDay[],
  range: HistoryRange = '90d',
): WalletValueHistory {
  const dates = daysEnding(days.length);
  return {
    wallet_id: walletId,
    asset,
    range,
    points: dates.map((day, index) => {
      const [quantity, value] = days[index] ?? [null, null];
      return { day, quantity, value };
    }),
  };
}

/** A wallet no run has read: no quantity and no value on any day of the range. */
export function unreadWalletHistory(
  walletId: number,
  asset: string,
  range: HistoryRange,
): WalletValueHistory {
  return walletValueHistory(
    walletId,
    asset,
    Array.from({ length: RANGE_DAYS[range] }, () => [null, null] as const),
    range,
  );
}

/** 29,000.00 USDT. */
export const VALUE_A = '29000.000000000000000000';
/** 29,500.00 USDT. */
export const VALUE_B = '29500.000000000000000000';
/** 30,770.00 USDT: the dashboard's `VALUED_SUMMARY` total, today. */
export const VALUE_TODAY = '30770.000000000000000000';

/**
 * Six days: two before anything was read, two valued, one with a price missing, and today.
 * Three runs of days, then: a gap, a line of two, a gap, and a lone day at the end.
 */
export const GAPPY_HISTORY: PortfolioHistory = portfolioHistory([
  null,
  null,
  VALUE_A,
  VALUE_B,
  null,
  VALUE_TODAY,
]);

/** Three days, every one valued. */
export const WHOLE_HISTORY: PortfolioHistory = portfolioHistory([VALUE_A, VALUE_B, VALUE_TODAY]);

/**
 * The value-history card named `name`, once its first load has settled: loaded, empty or
 * failed, but no longer loading.
 *
 * The card is found first, by its heading, which every state keeps; then its loading status
 * is waited out. A page's own "has it loaded" check needs this as well as its figures: the
 * chart's query starts when its card mounts, so its loading status can still be on screen
 * when the figures arrive, and a test asserting "no status" would then race it.
 */
export async function historySettled(name: string): Promise<HTMLElement> {
  const card = await screen.findByRole('region', { name });
  await waitFor(() => {
    expect(within(card).queryByRole('status')).not.toBeInTheDocument();
  });
  return card;
}
