import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GAPS_NOTE, type ChartSeries, type HistoryPoint } from '@/lib/history';
import { money } from '@/lib/money';
import { TOUCH_QUERY } from '@/lib/pointer';
import {
  HistoryTooltip,
  ValueHistoryChart,
  type ChartRow,
} from '@/pages/dashboard/ValueHistoryChart';
import { GAPPY_HISTORY, VALUE_A, VALUE_TODAY, WHOLE_HISTORY } from '@/test/historyFixtures';

/** A day as the chart builds it, the way Recharts hands it back on hover. */
const VALUED_POINT = {
  day: '2026-09-24',
  value: money(VALUE_TODAY),
  quantity: money('0.49950000'),
  y: 30770,
  marked: true,
};

const VALUED_DAY: ChartRow = {
  day: '2026-09-24',
  quantity: money('0.49950000'),
  lines: { value: VALUED_POINT },
};

const ONE_LINE = [{ key: 'value', label: 'Portfolio value', color: 'var(--series-blue)' }];

/** One line over `points`, as a wallet's chart draws it. */
function line(points: readonly HistoryPoint[], color = 'var(--series-other)'): ChartSeries[] {
  return [{ key: 'value', label: 'Portfolio value', color, points }];
}

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
    render(<HistoryTooltip active payload={[{ payload: VALUED_DAY }]} series={ONE_LINE} />);

    expect(screen.getByText('30,770.00 USDT').tagName).toBe('STRONG');
    expect(screen.getByText('Sep 24, 2026')).toBeInTheDocument();
    // A portfolio has no single asset, so no quantity.
    expect(screen.queryByText(/0\.4995/)).not.toBeInTheDocument();
  });

  it("adds a wallet's quantity, in its asset", () => {
    render(
      <HistoryTooltip active payload={[{ payload: VALUED_DAY }]} series={ONE_LINE} asset="BTC" />,
    );

    expect(screen.getByText('0.4995 BTC')).toBeInTheDocument();
  });

  it('says a gap could not be valued, and a wallet not yet read says so, rather than zero', () => {
    const { container } = render(
      <HistoryTooltip
        active
        payload={[
          {
            payload: {
              day: VALUED_DAY.day,
              quantity: null,
              lines: { value: { ...VALUED_POINT, value: null, quantity: null, y: null } },
            },
          },
        ]}
        series={ONE_LINE}
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
    const { container } = render(<HistoryTooltip {...props} series={ONE_LINE} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('ValueHistoryChart', () => {
  it('draws a gap as a gap: the line stops and starts again, and never drops to zero', () => {
    const { container } = render(
      <ValueHistoryChart
        series={line(GAPPY_HISTORY.points, 'var(--series-blue)')}
        caption="Portfolio value, the last 90 days"
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
        series={line(WHOLE_HISTORY.points)}
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
        series={line(WHOLE_HISTORY.points)}
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
        series={line([
          { day: '2026-09-23', value: null },
          { day: '2026-09-24', value: VALUE_TODAY },
        ])}
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
        series={line(WHOLE_HISTORY.points)}
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
        series={line([{ day: '2026-09-24', value: null }])}
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

describe('ValueHistoryChart with several lines (spec 041)', () => {
  const total = {
    key: 'total',
    label: 'Total',
    color: 'var(--series-blue)',
    points: WHOLE_HISTORY.points,
  };
  const btc = {
    key: 'BTC',
    label: 'BTC',
    color: 'var(--series-orange)',
    points: WHOLE_HISTORY.points.map(({ day }, index) => ({
      day,
      value: index === 0 ? null : VALUE_A,
    })),
  };
  const kas = {
    key: 'KAS',
    label: 'KAS',
    color: 'var(--series-aqua)',
    points: WHOLE_HISTORY.points.map(({ day }) => ({ day, value: null })),
  };

  it('draws one line per series in its colour, without the wash, and describes each', () => {
    const { container } = render(
      <ValueHistoryChart
        series={[total, btc, kas]}
        caption="Portfolio value"
        emptyText={EMPTY}
        busy={false}
      />,
    );

    const curves = [...container.querySelectorAll('.recharts-area-curve')];
    expect(curves.map((path) => path.getAttribute('stroke'))).toEqual([
      'var(--series-blue)',
      'var(--series-orange)',
      'var(--series-aqua)',
    ]);
    const washes = [...container.querySelectorAll('.recharts-area-area')];
    expect(washes.length).toBeGreaterThan(0);
    expect(washes.every((wash) => wash.getAttribute('fill-opacity') === '0')).toBe(true);
    expect(screen.getByRole('figure', { name: 'Portfolio value' })).toHaveTextContent(
      'Total: From 29,000.00 USDT on Sep 22, 2026 to 30,770.00 USDT on Sep 24, 2026. ' +
        'BTC: From 29,000.00 USDT on Sep 23, 2026 to 29,000.00 USDT on Sep 24, 2026. ' +
        'KAS: not valued on any day of this range.',
    );
    // BTC's first day is a gap in its own line.
    expect(screen.getByText(GAPS_NOTE)).toBeInTheDocument();
  });

  it('names each line beside its value in the hover card, the day first', () => {
    const row: ChartRow = {
      day: '2026-09-24',
      quantity: null,
      lines: {
        total: VALUED_POINT,
        BTC: { ...VALUED_POINT, value: null, y: null },
      },
    };
    render(<HistoryTooltip active payload={[{ payload: row }]} series={[total, btc, kas]} />);

    expect(screen.getByText('Sep 24, 2026').tagName).toBe('STRONG');
    expect(screen.getByText('Total: 30,770.00 USDT')).toBeInTheDocument();
    expect(screen.getByText('BTC: Not valued')).toBeInTheDocument();
    expect(screen.getByText('KAS: Not valued')).toBeInTheDocument();
  });

  it('has no chart when no line has a value', () => {
    render(
      <ValueHistoryChart series={[kas]} caption="Portfolio value" emptyText={EMPTY} busy={false} />,
    );

    expect(screen.getByText(EMPTY)).toBeInTheDocument();
  });

  describe('on a touch screen', () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    function renderOn(touch: boolean): HTMLElement {
      vi.stubGlobal('matchMedia', (media: string) => ({
        media,
        matches: touch && media === TOUCH_QUERY,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
      }));
      const { container } = render(
        <ValueHistoryChart
          series={line(WHOLE_HISTORY.points)}
          caption="Portfolio value"
          emptyText={EMPTY}
          busy={false}
        />,
      );
      const chart = container.querySelector('.history-chart');
      if (chart === null) {
        throw new Error('No chart was drawn.');
      }
      return chart as HTMLElement;
    }

    it('lifts the hover card above the chart, off the lines under the finger', () => {
      expect(renderOn(true)).toHaveClass('history-chart-touch');
    });

    it('leaves the hover card beside a mouse pointer', () => {
      expect(renderOn(false)).not.toHaveClass('history-chart-touch');
    });
  });

  it('has no chart when there are no lines at all', () => {
    render(
      <ValueHistoryChart series={[]} caption="Portfolio value" emptyText={EMPTY} busy={false} />,
    );

    expect(screen.getByText(EMPTY)).toBeInTheDocument();
  });
});
