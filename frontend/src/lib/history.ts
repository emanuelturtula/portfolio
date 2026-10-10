/**
 * The value history's points, words and formats (spec 037), without React: what the chart
 * draws, what it says about it, and how a day and an axis tick are written.
 *
 * A day nothing could value arrives as `null` and stays `null` all the way to the chart, which
 * leaves a gap there. It is never turned into `0`: a portfolio drawn at zero on the day a price
 * was missing is a crash that never happened, and it would be believed.
 */
import type { HistoryRange } from '@/api/history';
import { colorOf } from '@/lib/assetColors';
import { money, toChartNumber, type Money } from '@/lib/money';

/** One day as either endpoint serves it. A portfolio day carries no quantity. */
export interface HistoryPoint {
  readonly day: string;
  readonly value: string | null;
  readonly quantity?: string | null;
}

/** One line on a chart: what it is called, its colour, and its points, oldest first. */
export interface ChartSeries {
  /** Unique among the lines of one chart. */
  readonly key: string;
  /** What the line is, for the hover card and the sentence beside the chart: "BTC". */
  readonly label: string;
  /** A CSS colour such as `var(--series-blue)`. */
  readonly color: string;
  readonly points: readonly HistoryPoint[];
}

/** The portfolio history as the chart reads it: the total per day, and each asset's. */
export interface PortfolioSeriesData {
  readonly assets: readonly string[];
  readonly points: readonly {
    readonly day: string;
    readonly value: string | null;
    readonly assets: Readonly<Record<string, string | null | undefined>>;
  }[];
}

/** The key of the portfolio's total among its lines (spec 041). Never an asset's symbol. */
export const TOTAL_SERIES = 'total';

/** The total's line colour: the colour the chart had when it drew the total alone. */
export const TOTAL_COLOR = 'var(--series-blue)';

/**
 * The lines the portfolio chart draws: the total and each asset, in the order offered, keeping
 * only those `selected` names. An asset's day is its own value that day, `null` as served.
 */
export function portfolioSeries(
  data: PortfolioSeriesData,
  selected: readonly string[],
  colors: ReadonlyMap<string, string>,
): ChartSeries[] {
  const total: ChartSeries = {
    key: TOTAL_SERIES,
    label: 'Total',
    color: TOTAL_COLOR,
    points: data.points.map(({ day, value }) => ({ day, value })),
  };
  const assets = data.assets.map((asset): ChartSeries => ({
    key: asset,
    label: asset,
    color: colorOf(colors, asset),
    points: data.points.map(({ day, assets: values }) => ({ day, value: values[asset] ?? null })),
  }));
  return [total, ...assets].filter((series) => selected.includes(series.key));
}

/** One day as the chart holds it. */
export interface ChartPoint {
  /** The UTC day, `YYYY-MM-DD`, as served. */
  readonly day: string;
  /** The exact value, for every label and tooltip. `null` is a day nothing could value. */
  readonly value: Money | null;
  /** The wallet's quantity at the end of the day; `null` before its first reading. */
  readonly quantity: Money | null;
  /** `value` as a chart coordinate, never shown. See `toChartNumber`. `null` leaves a gap. */
  readonly y: number | null;
  /**
   * Whether the day carries a dot: the last day with a value, where the line ends, and any day
   * with a value between two gaps, which as a line of one point would draw nothing at all. A
   * wallet first read today is exactly that day.
   */
  readonly marked: boolean;
}

/** A day with a value. */
export interface ValuedDay {
  readonly day: string;
  readonly value: Money;
}

/** What a history adds up to, for the sentence beside the chart. */
export interface HistorySummary {
  readonly first: ValuedDay;
  readonly last: ValuedDay;
  /** How many days of the range could not be valued. */
  readonly gaps: number;
}

/** The selector's label for each range. */
export const RANGE_LABELS: Record<HistoryRange, string> = {
  '30d': '30D',
  '90d': '90D',
  '1y': '1Y',
  all: 'All',
};

/** Each range in words, for the chart's accessible name. */
export const RANGE_PHRASES: Record<HistoryRange, string> = {
  '30d': 'the last 30 days',
  '90d': 'the last 90 days',
  '1y': 'the last year',
  all: 'since the first reading',
};

/**
 * Shown beside a chart with gaps in it. A gap and a fall to zero look nothing alike on the
 * chart, but only a sentence says which of the two the reader is looking at.
 */
export const GAPS_NOTE =
  'Gaps are days that could not be valued: nothing had been read yet, or a price was missing ' +
  'that day.';

/**
 * Where a chart would be when no day of the range has a value. Not a failure, which says so in
 * an alert, and never a line at zero: nothing is known about those days yet.
 */
export const PORTFOLIO_EMPTY_WORDS =
  'No day in this range could be valued yet. The line starts on the first day a wallet has ' +
  'been read and its asset has a price.';

export const WALLET_EMPTY_WORDS =
  'No day in this range could be valued yet. The line starts on the first day this wallet has ' +
  'been read and its asset has a price.';

/** A day as the tooltip and the summary write it: "Oct 8, 2026". */
const DAY_FORMAT = new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' });

/** An axis tick over a span of a few months: "Oct 8". */
const SHORT_SPAN_TICK = new Intl.DateTimeFormat('en', {
  month: 'short',
  day: 'numeric',
  timeZone: 'UTC',
});

/** An axis tick over a longer span, where a day of the month is noise: "Oct 2026". */
const LONG_SPAN_TICK = new Intl.DateTimeFormat('en', {
  month: 'short',
  year: 'numeric',
  timeZone: 'UTC',
});

/** Past this many days, ticks name months rather than days. */
const LONG_SPAN_DAYS = 120;

/**
 * A value tick: "30K". A tick is a round number the axis picked to draw a gridline at, never
 * an amount anyone holds, and it is written compactly so it cannot be mistaken for one. Every
 * amount the chart shows is in the tooltip and the summary, formatted from its exact string.
 */
const AXIS_VALUE_FORMAT = new Intl.NumberFormat('en', {
  notation: 'compact',
  maximumFractionDigits: 1,
});

/**
 * The instant a served day begins. Read as UTC, because the backend's days are UTC days: read
 * in the browser's zone instead, every day west of Greenwich would be written as the day before.
 */
function dayStart(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

/** A day for a person to read: "Oct 8, 2026". */
export function formatDay(day: string): string {
  return DAY_FORMAT.format(dayStart(day));
}

/** The tick format for a chart of `days` days. */
export function axisDayFormat(days: number): (day: string) => string {
  const format = days > LONG_SPAN_DAYS ? LONG_SPAN_TICK : SHORT_SPAN_TICK;
  return (day) => format.format(dayStart(day));
}

/** A value tick, written compactly. See {@link AXIS_VALUE_FORMAT}. */
export function formatAxisValue(tick: number): string {
  return AXIS_VALUE_FORMAT.format(tick);
}

/** The served points as the chart holds them, in the same order (oldest first). */
export function toChartPoints(points: readonly HistoryPoint[]): ChartPoint[] {
  const days = points.map((point) => {
    const quantity = point.quantity ?? null;
    return {
      day: point.day,
      value: point.value === null ? null : money(point.value),
      quantity: quantity === null ? null : money(quantity),
    };
  });
  const valued = days.map((day) => day.value !== null);
  const lastValued = valued.lastIndexOf(true);

  return days.map((day, index) => {
    const alone = valued[index - 1] !== true && valued[index + 1] !== true;

    return {
      ...day,
      y: day.value === null ? null : toChartNumber(day.value),
      marked: day.value !== null && (alone || index === lastValued),
    };
  });
}

/** The first and last days with a value, and how many had none; `undefined` when none had one. */
export function summarize(points: readonly ChartPoint[]): HistorySummary | undefined {
  const valued = points.flatMap((point) =>
    point.value === null ? [] : [{ day: point.day, value: point.value }],
  );
  const [first, ...rest] = valued;

  if (first === undefined) {
    return undefined;
  }

  // The last of `rest`, or `first` when it is the only one: a fold rather than an index, which
  // the type would make possibly undefined although `first` already proves it is not.
  const last = rest.reduce((_previous, point) => point, first);

  return { first, last, gaps: points.length - valued.length };
}
