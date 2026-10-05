import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AllocationDonut, SliceTooltip } from '@/pages/dashboard/AllocationDonut';
import { BTC_HOLDING, KAS_HOLDING } from '@/test/summaryFixtures';

/** A slice as the donut builds it, the way Recharts hands it back on hover. */
const BTC_SLICE = {
  asset: 'BTC',
  share: BTC_HOLDING.share_pct,
  value: BTC_HOLDING.value,
  angle: 97.4001,
  fill: 'var(--series-orange)',
};

describe('SliceTooltip', () => {
  it('shows the hovered slice in exact, formatted figures', () => {
    render(<SliceTooltip active payload={[{ payload: BTC_SLICE }]} />);

    expect(screen.getByText('BTC')).toBeInTheDocument();
    expect(screen.getByText('29,970.00 USDT')).toBeInTheDocument();
    expect(screen.getByText('97.40 %')).toBeInTheDocument();
  });

  it.each([
    ['nothing is hovered', { active: false, payload: [{ payload: BTC_SLICE }] }],
    ['the hover has no slice under it', { active: true, payload: [] }],
    ['Recharts sends no payload at all', { active: true }],
  ])('renders nothing when %s', (_, props) => {
    const { container } = render(<SliceTooltip {...props} />);

    expect(container).toBeEmptyDOMElement();
  });
});

describe('AllocationDonut', () => {
  it('paints an asset missing from the colour map grey, rather than leaving it blank', () => {
    render(
      <AllocationDonut
        holdings={[BTC_HOLDING, KAS_HOLDING]}
        colors={new Map([['BTC', 'var(--series-orange)']])}
      />,
    );

    const legend = screen.getByRole('list');
    const kas = within(legend).getByText('KAS').previousElementSibling;
    expect(kas).toHaveStyle({ background: 'var(--series-other)' });
  });

  it('draws nothing when no holding has a share', () => {
    const { container } = render(
      <AllocationDonut
        holdings={[{ ...KAS_HOLDING, price: null, value: null, share_pct: null }]}
        colors={new Map()}
      />,
    );

    expect(container).toBeEmptyDOMElement();
  });
});
