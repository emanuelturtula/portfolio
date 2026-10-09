import type { ReactNode } from 'react';

import type { PortfolioSummary } from '@/api/portfolio';
import { Money } from '@/components/Money';
import { money } from '@/lib/money';
import { valueIsPartial } from '@/lib/portfolio';

const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;

interface TotalHeroProps {
  readonly summary: PortfolioSummary;
  /** The control beside the figure: the dashboard's Refresh. */
  readonly action: ReactNode;
  /** What is said about the figure, under it: what it is missing, and how a refresh went. */
  readonly children?: ReactNode;
}

/**
 * The dashboard's hero (spec 039): what everything held is worth, in USDT, set larger than
 * anything else on the page, with the Refresh that re-reads it beside it and what it could not
 * include under it.
 *
 * The label is the section's heading, so a screen reader lists the figure by name; the
 * "Partial" chip sits beside the heading rather than in it, so the name stays the figure's.
 *
 * When holdings exist and not one of them could be valued, the total is an empty sum rather
 * than a real amount, and presenting `0.00` would be a fabricated zero: the value shows a dash
 * instead.
 */
export function TotalHero({ summary, action, children }: TotalHeroProps) {
  const partial = valueIsPartial(summary.missing);
  const nothingValued =
    summary.holdings.length > 0 && summary.holdings.every((holding) => holding.value === null);

  return (
    <section className="kpi hero" aria-labelledby="kpi-value">
      <div className="kpi-head">
        <h2 id="kpi-value" className="kpi-label">
          Total value
        </h2>
        {partial && <span className="chip chip-warning">Partial</span>}
      </div>
      <div className="hero-row">
        <div className="kpi-value hero-value">
          {nothingValued ? (
            '—'
          ) : (
            <>
              <Money value={money(summary.total_value)} options={FIAT} />
              <span className="kpi-unit">USDT</span>
            </>
          )}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
