import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { GAPS_NOTE } from '@/lib/history';
import { money } from '@/lib/money';
import { HistoryTooltip, ValueHistoryChart } from '@/pages/dashboard/ValueHistoryChart';
import { GAPPY_HISTORY, VALUE_TODAY, WHOLE_HISTORY } from '@/test/historyFixtures';

/** A day as the chart builds it, the way Recharts hands it back on hover. */
const VALUED_DAY = {
  day: '2026-09-24',
  value: money(VALUE_TODAY),
  quantity: money('0.49950000'),
  y: 30770,
  marked: true,
};

const EMPTY = 'Nothing could be valued yet.';

/** The drawn line: one `M` per run of valued days, so a gap starts a new one. */
function curve(container: HTMLElement): string {
  const path = container.querySelector('.recharts-area-curve');
  if (path === null) {
    throw new Error('No line was drawn.');
  }
  return path.getAttribute('d') ?? '';
}

function runs(container: HTMLElement): number {
  return curve(container).split('M').length - 1;
}

describe('HistoryTooltip', () => {
  it("shows the hovered day's value in exact, formatted figures, then the day", () => {
    render(<HistoryTooltip active payload={[{ payload: VALUED_DAY }]} />);

    expect(screen.getByText('30,770.00 USDT').tagName).toBe('STRONG');
    expect(screen.getByText('Sep 24, 2026')).toBeInTheDocument();
    // A portfolio has no single asset, so no quantity.
    expect(screen.queryByText(/0\.4995/)).not.toBeInTheDocument();
  });

  it("adds a wallet's quantity, in its asset", () => {
    render(<HistoryTooltip active payload={[{ payload: VALUED_DAY }]} asset="BTC" />);

    expect(screen.getByText('0.4995 BTC')).toBeInTheDocument();
  });

  it('says a gap could not be valued, and a wallet not yet read says so, rather than zero', () => {
    const { container } = render(
      <HistoryTooltip
        active
        payload={[{ payload: { ...VALUED_DAY, value: null, quantity: null, y: null } }]}
        asset="BTC"
      />,
    );

    expect(screen.getByText('Not valued')).toBeInTheDocument();
    expect(screen.getByText('Not read yet')).toBeInTheDocument();
    expect(container).not.toHaveTextContent(/\b0(\.00)?\b/);
  });

  it.each([
    ['nothing is hovered', { active: false, payload: [{ payload: VALUED_DAY }] }],
    ['the hover has no day under it', { active: true, payload: [] }],
    ['Recharts sends no payload at all', { active: true }],
  ])('renders nothing when %s', (_, props) => {
    const { container } = render(<HistoryTooltip {...props} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('ValueHistoryChart', () => {
  it('draws a gap as a gap: the line stops and starts again, and never drops to zero', () => {
    const { container } = render(
      <ValueHistoryChart
        points={GAPPY_HISTORY.points}
        caption="Portfolio value, the last 90 days"
        color="var(--series-blue)"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    // [gap, gap, A, B, gap, today]: two runs of valued days. Drawn at zero, the gaps would join
    // them into one.
    expect(runs(container)).toBe(2);
    // Today stands alone after a gap, and ends the line: one dot, in the line's colour.
    const dots = container.querySelectorAll('.history-dot');
    expect(dots).toHaveLength(1);
    expect(dots[0]).toHaveAttribute('fill', 'var(--series-blue)');
    expect(screen.getByText(GAPS_NOTE)).toBeInTheDocument();
    expect(
      screen.getByRole('figure', { name: 'Portfolio value, the last 90 days' }),
    ).toHaveTextContent('From 29,000.00 USDT on Sep 21, 2026 to 30,770.00 USDT on Sep 24, 2026.');
  });

  it('says nothing about gaps when every day has a value', () => {
    const { container } = render(
      <ValueHistoryChart
        points={WHOLE_HISTORY.points}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    expect(runs(container)).toBe(1);
    expect(screen.queryByText(GAPS_NOTE)).not.toBeInTheDocument();
  });

  it('draws a line with no colour of its own in grey', () => {
    const { container } = render(
      <ValueHistoryChart
        points={WHOLE_HISTORY.points}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    expect(container.querySelector('.recharts-area-curve')).toHaveAttribute(
      'stroke',
      'var(--series-other)',
    );
  });

  it('describes a single valued day as one, not as a range from it to itself', () => {
    render(
      <ValueHistoryChart
        points={[
          { day: '2026-09-23', value: null },
          { day: '2026-09-24', value: VALUE_TODAY },
        ]}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    expect(screen.getByRole('figure', { name: 'Portfolio value' })).toHaveTextContent(
      'Valued on one day only: 30,770.00 USDT on Sep 24, 2026.',
    );
  });

  it('marks a range still loading as busy, keeping the one on screen', () => {
    render(
      <ValueHistoryChart
        points={WHOLE_HISTORY.points}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy
      />,
    );

    expect(screen.getByRole('figure', { name: 'Portfolio value' })).toHaveAttribute(
      'aria-busy',
      'true',
    );
  });

  it('has no chart, only its sentence, when no day has a value', () => {
    const { container } = render(
      <ValueHistoryChart
        points={[{ day: '2026-09-24', value: null }]}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    expect(screen.getByText(EMPTY)).toBeInTheDocument();
    expect(screen.queryByRole('figure')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(container.querySelector('svg')).toBeNull();
  });
});
