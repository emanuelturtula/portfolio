import { Money } from '@/components/Money';
import { SIGNED_FORMAT } from '@/lib/accounting';
import { money } from '@/lib/money';

interface ReturnPercentProps {
  /** The percentage as the backend sent it, e.g. `"71.4286"`, or `null` when it has none. */
  readonly value: string | null;
}

/**
 * A return percentage, signed and to two places: "+71.43%". "—" when there is none - a zero
 * or negative basis has no meaningful percentage, and a missing return is never a `0%`.
 * The exact string sits in the `<data value>`.
 */
export function ReturnPercent({ value }: ReturnPercentProps) {
  if (value === null) {
    return '—';
  }

  return (
    <>
      <Money value={money(value)} options={SIGNED_FORMAT} />%
    </>
  );
}
