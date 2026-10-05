import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ReturnDelta } from '@/pages/dashboard/ReturnDelta';

/** The pill, and the arrow inside it. */
function renderDelta(value: string | null) {
  const { container } = render(<ReturnDelta value={value} />);
  const pill = container.querySelector('.delta');
  return { container, pill, arrow: pill?.querySelector('[aria-hidden="true"]') ?? null };
}

describe('ReturnDelta', () => {
  it('renders nothing for a return that does not exist', () => {
    const { container } = renderDelta(null);

    expect(container).toBeEmptyDOMElement();
  });

  it('marks a gain with an up arrow and a plus, the exact figure in its data value', () => {
    const { pill, arrow } = renderDelta('71.4286');

    expect(pill).toHaveClass('delta', 'delta-gain');
    expect(pill).toHaveTextContent('▲+71.43%');
    expect(arrow).toHaveTextContent('▲');
    expect(pill?.querySelector('data')).toHaveAttribute('value', '71.4286');
  });

  it('marks a loss with a down arrow and a minus', () => {
    const { pill, arrow } = renderDelta('-12.5');

    expect(pill).toHaveClass('delta-loss');
    expect(pill).toHaveTextContent('▼-12.50%');
    expect(arrow).toHaveTextContent('▼');
  });

  it('leaves a break-even return unsigned, with no arrow', () => {
    const { pill, arrow } = renderDelta('0.0000');

    expect(pill).toHaveClass('delta-flat');
    expect(pill).toHaveTextContent(/^0\.00%$/);
    expect(arrow).toBeEmptyDOMElement();
  });

  it('keeps the percent sign with its figure, apart from the arrow', () => {
    // The pill lays out its children with a gap: the figure and its sign are one child, so the
    // gap never lands between "+30.00" and "%".
    const { pill } = renderDelta('30');

    expect(pill?.children).toHaveLength(2);
    expect(pill?.children[1]).toHaveTextContent(/^\+30\.00%$/);
  });
});
