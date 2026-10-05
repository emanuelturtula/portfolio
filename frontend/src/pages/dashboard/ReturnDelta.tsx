import { money, toneOf, type Tone } from '@/lib/money';
import { ReturnPercent } from '@/pages/dashboard/ReturnPercent';

const ARROWS: Record<Tone, string> = { gain: '▲', loss: '▼', flat: '' };

/**
 * A return as a small pill: the arrow and the sign say the direction, the colour is on top of
 * them. Nothing at all for a return that does not exist, such as one over nothing invested.
 */
export function ReturnDelta({ value }: { readonly value: string | null }) {
  if (value === null) {
    return null;
  }

  const tone = toneOf(money(value));
  return (
    <span className={`delta delta-${tone}`}>
      <span aria-hidden="true">{ARROWS[tone]}</span>
      <span>
        <ReturnPercent value={value} />
      </span>
    </span>
  );
}
