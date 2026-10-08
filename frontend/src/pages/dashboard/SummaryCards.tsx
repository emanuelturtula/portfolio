import type { PortfolioSummary } from '@/api/portfolio';
import { Money } from '@/components/Money';
import { money } from '@/lib/money';
import { valueIsPartial } from '@/lib/portfolio';

const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;

/**
 * The dashboard's figure: what everything held is worth, in USDT.
 *
 * The label is the section's heading, so a screen reader lists the figure by name; the
 * "Partial" chip sits beside the heading rather than in it, so the name stays the figure's.
 *
 * When holdings exist and not one of them could be valued, the total is an empty sum rather
 * than a real amount, and presenting `0.00` would be a fabricated zero: the value shows a dash
 * instead.
 */
export function SummaryCards({ summary }: { readonly summary: PortfolioSummary }) {
  const partial = valueIsPartial(summary.missing);
  const nothingValued =
    summary.holdings.length > 0 && summary.holdings.every((holding) => holding.value === null);

  return (
    <div className="kpi-row">
      <section className="kpi" aria-labelledby="kpi-value">
        <div className="kpi-head">
          <h2 id="kpi-value" className="kpi-label">
            Total value
          </h2>
          {partial && <span className="chip chip-warning">Partial</span>}
        </div>
        <div className="kpi-value">
          {nothingValued ? (
            '—'
          ) : (
            <>
              <Money value={money(summary.total_value)} options={FIAT} />
              <span className="kpi-unit">USDT</span>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
