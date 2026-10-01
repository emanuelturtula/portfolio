import { RelativeTime } from '@/components/RelativeTime';
import type { BalanceReading } from '@/lib/accounting';

/**
 * How old each reading under the comparison is: the comparison is as old as its oldest
 * input, and a difference that is a sync away from disappearing looks the same as one that
 * is not unless the page says how old the figures are. Not inside a live region, so the
 * relative phrase may tick.
 */
export function BalanceReadings({ readings }: { readonly readings: readonly BalanceReading[] }) {
  return (
    <>
      <p>Balances last read:</p>
      <ul className="holdings-readings">
        {readings.map((reading) => (
          <li key={reading.label}>
            {reading.label}: <RelativeTime value={reading.at} />
          </li>
        ))}
      </ul>
    </>
  );
}
