import type { ReactNode } from 'react';

import type { PortfolioSummary } from '@/api/portfolio';
import { Money } from '@/components/Money';
import { formatMoney, isNegativeMoney, isZeroMoney, money } from '@/lib/money';
import { investedIsPartial, valueIsPartial } from '@/lib/portfolio';

const FIAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 } as const;
const SIGNED_FIAT = { ...FIAT, signDisplay: 'exceptZero' } as const;

interface KpiProps {
  readonly id: string;
  readonly label: string;
  readonly partial: boolean;
  readonly tone?: 'gain' | 'loss' | undefined;
  readonly children: ReactNode;
}

/**
 * One figure. The label is the section's heading, so a screen reader lists the three figures
 * by name; the "Partial" chip sits beside the heading rather than in it, so the name stays
 * the figure's.
 */
function Kpi({ id, label, partial, tone, children }: KpiProps) {
  return (
    <section className={`kpi${tone === undefined ? '' : ` kpi-${tone}`}`} aria-labelledby={id}>
      <div className="kpi-head">
        <h2 id={id} className="kpi-label">
          {label}
        </h2>
        {partial && <span className="chip chip-warning">Partial</span>}
      </div>
      <div className="kpi-value">{children}</div>
    </section>
  );
}

function Usdt({ value, signed = false }: { readonly value: string; readonly signed?: boolean }) {
  return (
    <>
      <Money value={money(value)} options={signed ? SIGNED_FIAT : FIAT} />
      <span className="kpi-unit">USDT</span>
    </>
  );
}

function toneOf(pnl: string): 'gain' | 'loss' | undefined {
  const value = money(pnl);
  if (isZeroMoney(value)) {
    return undefined;
  }
  return isNegativeMoney(value) ? 'loss' : 'gain';
}

/**
 * The dashboard's three figures: what is held is worth, what went in, and the difference.
 *
 * Profit and loss carry a sign and an arrow, never colour alone. When holdings exist and not
 * one of them could be valued, the value is an empty sum rather than a real amount, and
 * presenting `0.00` would be a fabricated zero: the value and the P/L show a dash instead.
 */
export function SummaryCards({ summary }: { readonly summary: PortfolioSummary }) {
  const valuePartial = valueIsPartial(summary.missing);
  const investedPartial = investedIsPartial(summary.missing);
  const nothingValued =
    summary.holdings.length > 0 && summary.holdings.every((holding) => holding.value === null);
  const tone = nothingValued ? undefined : toneOf(summary.pnl);

  return (
    <div className="kpi-row">
      <Kpi id="kpi-value" label="Total value" partial={valuePartial}>
        {nothingValued ? '—' : <Usdt value={summary.total_value} />}
      </Kpi>
      <Kpi id="kpi-invested" label="Invested" partial={investedPartial}>
        <Usdt value={summary.invested} />
      </Kpi>
      <Kpi id="kpi-pnl" label="Profit / loss" partial={valuePartial || investedPartial} tone={tone}>
        {nothingValued ? (
          '—'
        ) : (
          <>
            <Usdt value={summary.pnl} signed />
            {summary.pnl_pct !== null && (
              <span className="kpi-delta">
                <span aria-hidden="true">{tone === 'loss' ? '▼' : tone === 'gain' ? '▲' : ''}</span>
                {formatMoney(money(summary.pnl_pct), SIGNED_FIAT)} %
              </span>
            )}
          </>
        )}
      </Kpi>
    </div>
  );
}
