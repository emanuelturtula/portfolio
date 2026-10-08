import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { BarChart, type BarRow, type BarSeries } from '@/components/BarChart';
import { money } from '@/lib/money';

const FORMAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

const TWO_SERIES: readonly BarSeries[] = [
  { name: 'Last week', shade: 'soft' },
  { name: 'Today', shade: 'solid' },
];

function renderChart(rows: readonly BarRow[], series: readonly BarSeries[] = []) {
  render(<BarChart title="Value of each wallet" series={series} rows={rows} format={FORMAT} />);
  return screen.getByRole('figure', { name: 'Value of each wallet' });
}

/** The plot rows, in order. */
function plotRows(figure: HTMLElement): HTMLElement[] {
  return Array.from(figure.querySelectorAll<HTMLElement>('.bar-row'));
}

/** The tracks of one plot row, in series order. */
function tracks(row: HTMLElement | undefined): HTMLElement[] {
  if (row === undefined) {
    throw new Error('No such row.');
  }
  return Array.from(row.querySelectorAll<HTMLElement>('.bar-track'));
}

describe('BarChart', () => {
  it('is a figure named by its title, its plot hidden from assistive technology', () => {
    const figure = renderChart([
      { key: '1', label: 'Cold storage', bars: [{ shade: 'solid', value: money('100') }] },
    ]);

    // The table beside every chart carries the figures; read twice, they are noise.
    expect(figure.querySelector('.bar-rows')).toHaveAttribute('aria-hidden', 'true');
    expect(within(figure).queryByText('Cold storage')).toBeInTheDocument();
  });

  it('draws every bar on one scale from zero, the longest to the end of its track', () => {
    const figure = renderChart([
      { key: '1', label: 'Cold storage', bars: [{ shade: 'solid', value: money('200') }] },
      { key: '2', label: 'Spending', bars: [{ shade: 'solid', value: money('50') }] },
    ]);

    const [cold, spending] = plotRows(figure);
    expect(tracks(cold)[0]?.style.getPropertyValue('--bar-length')).toBe('100%');
    expect(tracks(spending)[0]?.style.getPropertyValue('--bar-length')).toBe('25%');
    expect(tracks(spending)[0]).toHaveClass('bar-track', 'bar-solid');
  });

  it('writes each figure at the tip of its bar, formatted from the exact string', () => {
    const figure = renderChart([
      {
        key: '1',
        label: 'Cold storage',
        bars: [{ shade: 'solid', value: money('84419.745000000000000001') }],
      },
    ]);

    expect(tracks(plotRows(figure)[0])[0]?.querySelector('.bar-value')).toHaveTextContent(
      '84,419.75',
    );
  });

  it('paints a row in its colour, and leaves the stylesheet default for a row with none', () => {
    const figure = renderChart([
      {
        key: '1',
        label: 'BTC',
        color: 'var(--series-orange)',
        bars: [{ shade: 'solid', value: money('1') }],
      },
      { key: '2', label: 'DOGE', bars: [{ shade: 'solid', value: money('1') }] },
    ]);

    const [btc, doge] = plotRows(figure);
    expect(btc?.style.getPropertyValue('--bar-color')).toBe('var(--series-orange)');
    expect(doge?.style.getPropertyValue('--bar-color')).toBe('');
  });

  it('shows a dash and no bar for a figure that does not exist', () => {
    const figure = renderChart([
      {
        key: 'ETH',
        label: 'ETH',
        bars: [
          { shade: 'soft', value: money('10') },
          { shade: 'solid', value: null },
        ],
      },
    ]);

    const [lastWeek, value] = tracks(plotRows(figure)[0]);
    expect(lastWeek?.querySelector('.bar')).not.toBeNull();
    expect(value?.querySelector('.bar')).toBeNull();
    expect(value).toHaveTextContent('—');
    expect(value?.getAttribute('style')).toBeNull();
    expect(value).not.toHaveClass('bar-solid');
  });

  it('draws no bar for a zero, but still writes the zero', () => {
    const figure = renderChart([
      { key: '1', label: 'Empty', bars: [{ shade: 'solid', value: money('0') }] },
      { key: '2', label: 'Full', bars: [{ shade: 'solid', value: money('5') }] },
    ]);

    const empty = tracks(plotRows(figure)[0])[0];
    expect(empty?.querySelector('.bar')).toBeNull();
    expect(empty?.style.getPropertyValue('--bar-length')).toBe('0%');
    expect(empty).toHaveTextContent('0.00');
  });

  it('puts a row aside under its label', () => {
    const figure = renderChart([
      {
        key: '1',
        label: 'BTC',
        bars: [{ shade: 'solid', value: money('1') }],
        aside: <span className="bar-sub">+30.00%</span>,
      },
    ]);

    expect(figure.querySelector('.bar-label .bar-sub')).toHaveTextContent('+30.00%');
  });

  it('has no legend for a single series: the heading already names it', () => {
    const figure = renderChart(
      [{ key: '1', label: 'BTC', bars: [{ shade: 'solid', value: money('1') }] }],
      [{ name: 'Value', shade: 'solid' }],
    );

    expect(within(figure).queryByRole('list')).not.toBeInTheDocument();
  });

  it('has a legend for two series, each swatch in its shade, outside the hidden plot', () => {
    const figure = renderChart(
      [
        {
          key: 'BTC',
          label: 'BTC',
          bars: [
            { shade: 'soft', value: money('1') },
            { shade: 'solid', value: money('2') },
          ],
        },
      ],
      TWO_SERIES,
    );

    const legend = within(figure).getByRole('list');
    const entries = within(legend).getAllByRole('listitem');
    expect(entries.map((entry) => entry.textContent)).toEqual(['Last week', 'Today']);
    expect(entries[0]?.querySelector('.swatch')).toHaveClass('swatch-soft');
    expect(entries[1]?.querySelector('.swatch')).toHaveClass('swatch-solid');

    const [soft, solid] = tracks(plotRows(figure)[0]);
    expect(soft).toHaveClass('bar-soft');
    expect(solid).toHaveClass('bar-solid');
  });
});
