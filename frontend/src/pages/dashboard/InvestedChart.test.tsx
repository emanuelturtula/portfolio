import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Positions } from '@/api/accounting';
import { InvestedChart } from '@/pages/dashboard/InvestedChart';
import {
  ethUnpriced,
  everyHeldPositionExcluded,
  investedPortfolio,
  kasLossPortfolio,
  position,
} from '@/test/accountingFixtures';

function renderChart(data: Positions) {
  return render(
    <InvestedChart
      positions={data.positions}
      excluded={data.totals.excluded}
      quoteCurrency="USDT"
    />,
  );
}

/** The plot rows of the chart, in order. */
function barRows(): HTMLElement[] {
  return Array.from(
    screen
      .getByRole('region', { name: 'Invested vs value' })
      .querySelectorAll<HTMLElement>('.bar-row'),
  );
}

describe('InvestedChart', () => {
  it('draws only the positions the totals are made of: held, and not left out', () => {
    // The portfolio holds BTC, ETH, KAS and SOL and has closed XRP; ETH, KAS and SOL are left
    // out of the totals, so the chart and the figures above it add up to the same thing.
    renderChart(investedPortfolio());

    const rows = barRows();
    expect(rows.map((row) => row.querySelector('.bar-name')?.textContent)).toEqual(['BTC']);
  });

  it('puts what went in beside what it is worth, the return under the name', () => {
    renderChart(investedPortfolio());

    const region = screen.getByRole('region', { name: 'Invested vs value' });
    expect(region).toHaveTextContent('USDT');
    const legend = within(region).getByRole('list');
    expect(
      within(legend)
        .getAllByRole('listitem')
        .map((entry) => entry.textContent),
    ).toEqual(['Invested', 'Market value']);

    const [btc] = barRows();
    const values = Array.from(btc?.querySelectorAll('.bar-value') ?? []).map(
      (value) => value.textContent,
    );
    expect(values).toEqual(['52,500.00', '90,000.00']);
    expect(btc?.querySelector('.delta')).toHaveTextContent('+71.43%');
    expect(btc?.querySelector('.delta')).toHaveClass('delta-gain');
  });

  it('marks a loss as one, with the shorter bar on the value side', () => {
    renderChart(kasLossPortfolio());

    const kas = barRows().find((row) => row.querySelector('.bar-name')?.textContent === 'KAS');
    expect(kas?.querySelector('.delta')).toHaveClass('delta-loss');
    const [invested, value] = Array.from(kas?.querySelectorAll<HTMLElement>('.bar-track') ?? []);
    expect(invested?.style.getPropertyValue('--bar-length')).toBe('100%');
    expect(value?.style.getPropertyValue('--bar-length')).not.toBe('100%');
  });

  it('keeps an asset in the colour it has with every position present, when another is left out', () => {
    // ETH and LTC have no colour of their own: over both, ETH takes the spare and LTC the
    // neutral grey. ETH is left out of the totals, and LTC must not take its colour for that.
    const ltc = position({ asset: 'LTC' });
    render(
      <InvestedChart
        positions={[ethUnpriced(), ltc]}
        excluded={[{ asset: 'ETH', reason: 'unpriced' }]}
        quoteCurrency="USDT"
      />,
    );

    const [row] = barRows();
    expect(row?.querySelector('.bar-name')?.textContent).toBe('LTC');
    expect(row?.style.getPropertyValue('--bar-color')).toBe('var(--series-other)');
  });

  it('draws nothing when no held position is counted', () => {
    const { container } = renderChart(everyHeldPositionExcluded());

    expect(container).toBeEmptyDOMElement();
  });
});
