import type { Exclusion, Position } from '@/api/accounting';
import { BarChart, type BarRow, type BarSeries } from '@/components/BarChart';
import { AMOUNT_FORMAT, isHeld } from '@/lib/accounting';
import { assetColors } from '@/lib/assetColors';
import { money, moneyOrNull } from '@/lib/money';
import { ReturnDelta } from '@/pages/dashboard/ReturnDelta';

const SERIES: readonly BarSeries[] = [
  { name: 'Invested', shade: 'soft' },
  { name: 'Market value', shade: 'solid' },
];

interface InvestedChartProps {
  readonly positions: readonly Position[];
  readonly excluded: readonly Exclusion[];
  /** The label the figures are in, already `USDT` for a USD snapshot. */
  readonly quoteCurrency: string;
}

/**
 * What went into each held position against what it is worth now, the return under its name:
 * the before and after of every asset, on one scale.
 *
 * Only the positions the totals are made of take a row, so the chart and the figures above it
 * add up to the same thing. A position left out of the totals is still a row of the table
 * below, with the reason it was left out. With none counted there is no chart.
 *
 * Colours are dealt over every position, not only the rows drawn: an asset with no colour of
 * its own takes a spare by its place among all of them, so leaving one out of the totals never
 * repaints another.
 */
export function InvestedChart({ positions, excluded, quoteCurrency }: InvestedChartProps) {
  const excludedAssets = new Set(excluded.map((entry) => entry.asset));
  const counted = positions.filter(
    (position) => isHeld(position) && !excludedAssets.has(position.asset),
  );

  if (counted.length === 0) {
    return null;
  }

  const colors = assetColors(positions.map((position) => position.asset));
  const rows: BarRow[] = counted.map((position) => ({
    key: position.asset,
    label: position.asset,
    color: colors.get(position.asset),
    bars: [
      { shade: 'soft', value: money(position.total_invested) },
      { shade: 'solid', value: moneyOrNull(position.market_value) },
    ],
    aside: <ReturnDelta value={position.unrealized_return_pct} />,
  }));

  return (
    <section className="card" aria-labelledby="invested-chart-heading">
      <div className="card-head">
        <h3 id="invested-chart-heading">Invested vs value</h3>
        <span className="page-meta">{quoteCurrency}</span>
      </div>
      <BarChart
        title="Invested and market value of each position"
        series={SERIES}
        rows={rows}
        format={AMOUNT_FORMAT}
      />
    </section>
  );
}
