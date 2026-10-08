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
  type HistoryPoint,
  type HistorySummary,
} from '@/lib/history';
import { formatMoney, type Money } from '@/lib/money';

const HEIGHT = 240;
const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const QUANTITY = { maximumFractionDigits: 8 } as const;
const UNIT = 'USDT';
/** The line of an entity with no colour of its own. */
const OTHER = 'var(--series-other)';

const AXIS_TICK = { fill: 'var(--color-muted)', fontSize: 12 } as const;

function amount(value: Money): string {
  return `${formatMoney(value, FIAT)} ${UNIT}`;
}

/**
 * The part of Recharts' tooltip props the hover card reads: whether the crosshair is on a day,
 * and the datum behind it. Narrower than `TooltipContentProps`, which Recharts' own props
 * satisfy, so a test can hand the card a day without building a whole chart's state.
 */
export interface HistoryTooltipProps {
  readonly active?: boolean | undefined;
  readonly payload?: readonly { readonly payload?: unknown }[] | undefined;
  /** The wallet's asset, for the quantity line. A portfolio has no single asset, and no line. */
  readonly asset?: string | undefined;
}

/**
 * The hover card: the day's exact value, formatted from its string, then the day. A gap says it
 * could not be valued rather than showing nothing, so a reader whose crosshair lands in one
 * learns why the line is missing there.
 */
export function HistoryTooltip({ active, payload, asset }: HistoryTooltipProps) {
  const point = payload?.[0]?.payload as ChartPoint | undefined;
  if (!active || point === undefined) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <strong>{point.value === null ? 'Not valued' : amount(point.value)}</strong>
      <span>{formatDay(point.day)}</span>
      {asset !== undefined && (
        <span>
          {point.quantity === null
            ? 'Not read yet'
            : `${formatMoney(point.quantity, QUANTITY)} ${asset}`}
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
function markedDot(color: string) {
  return function MarkedDot({ cx, cy, payload }: DotItemDotProps) {
    if (!(payload as ChartPoint).marked) {
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

/** The chart in one sentence, for a screen reader: it is all the chart says without hovering. */
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
  readonly points: readonly HistoryPoint[];
  /** The figure's accessible name: what is drawn and over which range. */
  readonly caption: string;
  /** The line's colour, a CSS colour such as `var(--series-blue)`. Grey without one. */
  readonly color?: string | undefined;
  /** The wallet's asset, when the points carry its quantity. */
  readonly asset?: string | undefined;
  /** What stands in the chart's place when no day of the range has a value. */
  readonly emptyText: string;
  /** Whether these points are a previous range's, shown while the chosen one loads. */
  readonly busy: boolean;
}

/**
 * Value over time, one point per day, as an area from zero.
 *
 * A day with no value is a gap: the area stops before it and starts again after it
 * (`connectNulls={false}`), and nothing is drawn at zero. With no valued day at all there is
 * no chart, only a sentence that says when the line will start - which is not an error, and
 * does not look like one. With some days missing, a note under the chart says what a gap is.
 *
 * The chart itself is `aria-hidden` and nothing in it takes focus: the figure's caption names
 * it and a sentence beside it says where the line starts and ends, in exact figures. Its width
 * follows the card (`responsive`); in a test, which cannot measure, it draws at a fixed size.
 */
export function ValueHistoryChart({
  points,
  caption,
  color,
  asset,
  emptyText,
  busy,
}: ValueHistoryChartProps) {
  const captionId = useId();
  const data = toChartPoints(points);
  const summary = summarize(data);

  if (summary === undefined) {
    return <p className="history-empty">{emptyText}</p>;
  }

  const line = color ?? OTHER;

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
              content={<HistoryTooltip asset={asset} />}
              filterNull={false}
              cursor={{ stroke: 'var(--color-faint)', strokeWidth: 1 }}
              isAnimationActive={false}
            />
            <Area
              type="linear"
              dataKey="y"
              connectNulls={false}
              stroke={line}
              strokeWidth={2}
              fill={line}
              fillOpacity={0.1}
              dot={markedDot(line)}
              activeDot={{ r: 4, fill: line, stroke: 'var(--color-surface)', strokeWidth: 2 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </div>
        <p className="visually-hidden">{describe(summary)}</p>
      </figure>
      {summary.gaps > 0 && <p className="note">{GAPS_NOTE}</p>}
    </>
  );
}
