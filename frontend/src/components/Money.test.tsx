import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Money } from '@/components/Money';
import { money } from '@/lib/money';

/** The `<data>` element `<Money>` renders into. */
function dataElement(container: HTMLElement): HTMLDataElement {
  const element = container.querySelector('data');

  if (element === null) {
    throw new Error(
      `<Money> did not render a <data> element, so the exact amount is not in the DOM. Markup was: ${container.innerHTML}`,
    );
  }

  return element;
}

describe('Money', () => {
  it('carries the unformatted amount in the data value attribute', () => {
    // Eighteen decimals with a six-digit integer part: twenty-four significant
    // digits, which the decimal.js default precision of twenty cannot hold.
    const exact = '123456.123456789012345678';

    const { container } = render(<Money value={money(exact)} />);

    // Asserting on the attribute rather than on the visible text is what makes
    // "no precision loss" checkable. The visible text is allowed to be grouped
    // and truncated; the attribute is not allowed to lose a digit.
    expect(dataElement(container).getAttribute('value')).toBe(exact);
  });

  it('keeps the smallest representable base unit intact', () => {
    const { container } = render(<Money value={money('0.000000000000000001')} />);

    expect(dataElement(container).getAttribute('value')).toBe('0.000000000000000001');
  });

  it('keeps a negative amount intact', () => {
    const { container } = render(<Money value={money('-0.000000000000000001')} />);

    expect(dataElement(container).getAttribute('value')).toBe('-0.000000000000000001');
  });

  it('shows something a human can read', () => {
    const { container } = render(<Money value={money('1234.5')} />);

    const element = dataElement(container);
    expect(element.textContent.trim().length).toBeGreaterThan(0);
    // Never exponent notation, whatever the grouping and truncation rules are.
    expect(element.textContent).not.toMatch(/e[+-]/i);
  });

  // The `value` attribute alone proves nothing about formatting: it is the
  // input string echoed straight back, and every assertion on it would still
  // pass with `decimal.js` removed from this component's path entirely. These
  // pin the visible text, which is the part a person actually reads.
  it('renders the formatted text, grouped', () => {
    const { container } = render(<Money value={money('1234567.89')} />);

    expect(dataElement(container).textContent).toBe('1,234,567.89');
  });

  it('keeps the sign in the visible text, not only in the attribute', () => {
    const { container } = render(<Money value={money('-1234.5')} />);

    const element = dataElement(container);
    expect(element.textContent).toBe('-1,234.5');
    expect(element.getAttribute('value')).toBe('-1234.5');
  });

  it('passes formatting options through to formatMoney', () => {
    const { container } = render(
      <Money value={money('1234.5')} options={{ minimumFractionDigits: 2 }} />,
    );

    const element = dataElement(container);
    expect(element.textContent).toBe('1,234.50');
    // Formatting is display only. The attribute still carries the exact input.
    expect(element.getAttribute('value')).toBe('1234.5');
  });

  it('respects a reduced maximum precision without losing the exact value', () => {
    const { container } = render(
      <Money value={money('1.2345')} options={{ maximumFractionDigits: 2 }} />,
    );

    const element = dataElement(container);
    expect(element.textContent).toBe('1.23');
    expect(element.getAttribute('value')).toBe('1.2345');
  });
});
