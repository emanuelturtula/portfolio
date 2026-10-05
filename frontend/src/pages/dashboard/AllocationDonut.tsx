import { Pie, PieChart, Tooltip } from 'recharts';

import type { Holding } from '@/api/portfolio';
import { formatMoney, isZeroMoney, money, toChartNumber, type Money } from '@/lib/money';

const SIZE = 200;
const PERCENT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const FIAT = PERCENT;

interface Slice {
  readonly asset: string;
  /** The share as the backend sent it: what every label shows. */
  readonly share: Money;
  readonly value: Money;
  /** The share as a chart coordinate, never shown. See `toChartNumber`. */
  readonly angle: number;
  /** The asset's colour. Recharts paints a sector with its datum's `fill`. */
  readonly fill: string;
}

function slicesOf(holdings: readonly Holding[], colors: ReadonlyMap<string, string>): Slice[] {
  return holdings.flatMap((holding) => {
    if (holding.share_pct === null || holding.value === null) {
      return [];
    }
    const share = money(holding.share_pct);
    if (isZeroMoney(share)) {
      return [];
    }
    return [
      {
        asset: holding.asset,
        share,
        value: money(holding.value),
        angle: toChartNumber(share),
        fill: colors.get(holding.asset) ?? 'var(--series-other)',
      },
    ];
  });
}

function percent(share: Money): string {
  return `${formatMoney(share, PERCENT)} %`;
}

/**
 * The part of Recharts' tooltip props the hover card reads: whether a slice is hovered, and the
 * datum behind it. Narrower than `TooltipContentProps`, which Recharts' own props satisfy, so a
 * test can hand the card a slice without building a whole chart's state.
 */
export interface SliceTooltipProps {
  readonly active?: boolean | undefined;
  readonly payload?: readonly { readonly payload?: unknown }[] | undefined;
}

/** The hover card: the exact strings, formatted, never the coordinate the arc was drawn at. */
export function SliceTooltip({ active, payload }: SliceTooltipProps) {
  const slice = payload?.[0]?.payload as Slice | undefined;
  if (!active || slice === undefined) {
    return null;
  }
  return (
    <div className="chart-tooltip">
      <strong>{slice.asset}</strong>
      <span>{formatMoney(slice.value, FIAT)} USDT</span>
      <span>{percent(slice.share)}</span>
    </div>
  );
}

interface AllocationDonutProps {
  readonly holdings: readonly Holding[];
  readonly colors: ReadonlyMap<string, string>;
}

/**
 * How the total value splits across the holdings: a donut, with a legend that carries every
 * share as text, so no reading depends on telling two colours apart.
 *
 * Only holdings with a value take a slice. An unpriced one has no share to draw, and a slice
 * of zero would be an arc nobody can see; both are still rows in the holdings table beside it.
 * The chart itself is `aria-hidden`, and nothing in it takes focus: the legend says the same
 * thing in words, and a focusable element a screen reader is told to skip is a trap.
 */
export function AllocationDonut({ holdings, colors }: AllocationDonutProps) {
  const slices = slicesOf(holdings, colors);

  if (slices.length === 0) {
    return null;
  }

  return (
    <figure className="donut" aria-labelledby="allocation-caption">
      <figcaption id="allocation-caption" className="visually-hidden">
        Allocation by value
      </figcaption>
      <div className="donut-chart" aria-hidden="true">
        <PieChart width={SIZE} height={SIZE} accessibilityLayer={false}>
          <Pie
            data={slices}
            dataKey="angle"
            nameKey="asset"
            innerRadius={SIZE * 0.33}
            outerRadius={SIZE * 0.48}
            startAngle={90}
            endAngle={-270}
            stroke="var(--color-surface)"
            // The 2px gap that keeps two fills apart; a lone slice has no neighbour, and the
            // gap would only cut a seam into the ring where it starts.
            strokeWidth={slices.length > 1 ? 2 : 0}
            isAnimationActive={false}
            rootTabIndex={-1}
          />
          <Tooltip content={SliceTooltip} />
        </PieChart>
      </div>
      <ul className="legend">
        {slices.map((slice) => (
          <li key={slice.asset}>
            <span className="swatch" style={{ background: slice.fill }} aria-hidden="true" />
            <span className="legend-asset">{slice.asset}</span>
            <span className="legend-share">{percent(slice.share)}</span>
          </li>
        ))}
      </ul>
    </figure>
  );
}
