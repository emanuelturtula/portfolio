import { useId } from 'react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  Tooltip,
  XAxis,
  YAxis,
  type DotItemDotProps,
} from 'recharts';

import {
  axisDayFormat,
  formatAxisValue,
  formatDay,
  GAPS_NOTE,
  summarize,
  toChartPoints,
  type ChartPoint,
  type ChartSeries,
  type HistorySummary,
} from '@/lib/history';
import { formatMoney, type Money } from '@/lib/money';

const HEIGHT = 240;
const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const QUANTITY = { maximumFractionDigits: 8 } as const;
const UNIT = 'USDT';

const AXIS_TICK = { fill: 'var(--color-muted)', fontSize: 12 } as const;

function amount(value: Money): string {
  return `${formatMoney(value, FIAT)} ${UNIT}`;
}

/**
 * One day as the chart holds it: the day, the quantity of the first line (a wallet's, when the
 * chart is one wallet), and each line's point by its key.
 */
export interface ChartRow {
  readonly day: string;
  readonly quantity: ChartPoint['quantity'];
  readonly lines: Readonly<Record<string, ChartPoint | undefined>>;
}

/** What the hover card needs of a line: its key, its name and its colour. */
export type SeriesLabel = Pick<ChartSeries, 'key' | 'label' | 'color'>;

/**
 * The part of Recharts' tooltip props the hover card reads: whether the crosshair is on a day,
 * and the datum behind it. Narrower than `TooltipContentProps`, which Recharts' own props
 * satisfy, so a test can hand the card a day without building a whole chart's state.
 */
export interface HistoryTooltipProps {
  readonly active?: boolean | undefined;
  readonly payload?: readonly { readonly payload?: unknown }[] | undefined;
  /** The lines drawn, in order. With one, the card shows its value alone, as before spec 041. */
  readonly series: readonly SeriesLabel[];
  /** The wallet's asset, for the quantity line. A portfolio has no single asset, and no line. */
  readonly asset?: string | undefined;
}

/**
 * The hover card: the day's exact value, formatted from its string, then the day. With several
 * lines it names each one beside its value, in the line's colour. A gap says it could not be
 * valued rather than showing nothing, so a reader whose crosshair lands in one learns why the
 * line is missing there.
 */
export function HistoryTooltip({ active, payload, series, asset }: HistoryTooltipProps) {
  const row = payload?.[0]?.payload as ChartRow | undefined;
  if (!active || row === undefined) {
    return null;
  }
  const valueOf = (key: string) => {
    const value = row.lines[key]?.value ?? null;
    return value === null ? 'Not valued' : amount(value);
  };
  const [only] = series;
  return (
    <div className="chart-tooltip">
      {series.length === 1 && only !== undefined ? (
        <>
          <strong>{valueOf(only.key)}</strong>
          <span>{formatDay(row.day)}</span>
        </>
      ) : (
        <>
          <strong>{formatDay(row.day)}</strong>
          {series.map((line) => (
            <span key={line.key} className="chart-tooltip-line">
              <span
                className="series-swatch"
                style={{ backgroundColor: line.color }}
                aria-hidden="true"
              />
              {line.label}: {valueOf(line.key)}
            </span>
          ))}
        </>
      )}
      {asset !== undefined && (
        <span>
          {row.quantity === null
            ? 'Not read yet'
            : `${formatMoney(row.quantity, QUANTITY)} ${asset}`}
        </span>
      )}
    </div>
  );
}

/**
 * A dot on the days `toChartPoints` marks, in the line's colour with a ring of the surface
 * around it: where the line ends, and any valued day between two gaps, which a line alone
 * could not draw.
 */
function markedDot(key: string, color: string) {
  return function MarkedDot({ cx, cy, payload }: DotItemDotProps) {
    if ((payload as ChartRow).lines[key]?.marked !== true) {
      return null;
    }
    return (
      <circle
        className="history-dot"
        cx={cx}
        cy={cy}
        r={4}
        fill={color}
        stroke="var(--color-surface)"
        strokeWidth={2}
      />
    );
  };
}

/** One line in one sentence, for a screen reader: it is all the chart says without hovering. */
function describe({ first, last }: HistorySummary): string {
  if (first.day === last.day) {
    return `Valued on one day only: ${amount(last.value)} on ${formatDay(last.day)}.`;
  }
  return (
    `From ${amount(first.value)} on ${formatDay(first.day)} ` +
    `to ${amount(last.value)} on ${formatDay(last.day)}.`
  );
}

interface ValueHistoryChartProps {
  /** The lines to draw, each with its own points over the same days. One for a wallet. */
  readonly series: readonly ChartSeries[];
  /** The figure's accessible name: what is drawn and over which range. */
  readonly caption: string;
  /** The wallet's asset, when the points carry its quantity. */
  readonly asset?: string | undefined;
  /** What stands in the chart's place when no day of the range has a value. */
  readonly emptyText: string;
  /** Whether these points are a previous range's, shown while the chosen one loads. */
  readonly busy: boolean;
}

/** The lines' points side by side, one row per day: the days of `first`, which every line shares. */
function toRows(
  first: readonly ChartPoint[],
  lines: readonly { key: string; points: readonly ChartPoint[] }[],
): ChartRow[] {
  return first.map((point, index) => ({
    day: point.day,
    quantity: point.quantity,
    lines: Object.fromEntries(lines.map((line) => [line.key, line.points[index]])),
  }));
}

/**
 * Value over time, one point per day, as an area from zero, one line per series.
 *
 * A day with no value is a gap: the area stops before it and starts again after it
 * (`connectNulls={false}`), and nothing is drawn at zero. With no valued day in any line there
 * is no chart, only a sentence that says when the line will start - which is not an error, and
 * does not look like one. With some days missing, a note under the chart says what a gap is.
 * Several lines are drawn without their wash, so one does not hide another (spec 041).
 *
 * The chart itself is `aria-hidden` and nothing in it takes focus: the figure's caption names
 * it and a sentence beside it says where each line starts and ends, in exact figures. Its width
 * follows the card (`responsive`); in a test, which cannot measure, it draws at a fixed size.
 */
export function ValueHistoryChart({
  series,
  caption,
  asset,
  emptyText,
  busy,
}: ValueHistoryChartProps) {
  const captionId = useId();
  const lines = series.map((line) => {
    const points = toChartPoints(line.points);
    return { ...line, points, summary: summarize(points) };
  });
  const valued = lines.flatMap((line) =>
    line.summary === undefined ? [] : [{ ...line, summary: line.summary }],
  );

  const [firstValued] = valued;
  if (firstValued === undefined) {
    return <p className="history-empty">{emptyText}</p>;
  }

  const data = toRows(firstValued.points, lines);
  const single = lines.length === 1;
  const sentence = single
    ? describe(firstValued.summary)
    : lines
        .map((line) =>
          line.summary === undefined
            ? `${line.label}: not valued on any day of this range.`
            : `${line.label}: ${describe(line.summary)}`,
        )
        .join(' ');

  return (
    <>
      <figure className="history" aria-labelledby={captionId} aria-busy={busy}>
        <figcaption id={captionId} className="visually-hidden">
          {caption}
        </figcaption>
        <div className="history-chart" aria-hidden="true">
          <AreaChart
            responsive
            width="100%"
            height={HEIGHT}
            data={data}
            margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
            accessibilityLayer={false}
          >
            <CartesianGrid vertical={false} stroke="var(--color-hairline)" />
            <XAxis
              dataKey="day"
              tickFormatter={axisDayFormat(data.length)}
              minTickGap={24}
              tickLine={false}
              axisLine={{ stroke: 'var(--color-hairline)' }}
              tick={AXIS_TICK}
            />
            <YAxis
              domain={[0, 'auto']}
              tickFormatter={formatAxisValue}
              width={44}
              tickLine={false}
              axisLine={false}
              tick={AXIS_TICK}
            />
            <Tooltip
              content={<HistoryTooltip series={lines} asset={asset} />}
              filterNull={false}
              cursor={{ stroke: 'var(--color-faint)', strokeWidth: 1 }}
              isAnimationActive={false}
            />
            {lines.map((line) => (
              <Area
                key={line.key}
                type="linear"
                dataKey={(row: ChartRow) => row.lines[line.key]?.y ?? null}
                name={line.label}
                connectNulls={false}
                stroke={line.color}
                strokeWidth={2}
                fill={line.color}
                fillOpacity={single ? 0.1 : 0}
                dot={markedDot(line.key, line.color)}
                activeDot={{
                  r: 4,
                  fill: line.color,
                  stroke: 'var(--color-surface)',
                  strokeWidth: 2,
                }}
                isAnimationActive={false}
              />
            ))}
          </AreaChart>
        </div>
        <p className="visually-hidden">{sentence}</p>
      </figure>
      {valued.some((line) => line.summary.gaps > 0) && <p className="note">{GAPS_NOTE}</p>}
    </>
  );
}
