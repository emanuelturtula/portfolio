import type { ReactElement } from 'react';

import type { AccountingWarning } from '@/api/accounting';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { Money } from '@/components/Money';
import { venueLabel } from '@/lib/accounting';
import { money } from '@/lib/money';

/**
 * One sentence per warning kind. A `switch` with an explicit return type rather than a
 * ternary, so a kind added on the backend is a `tsc` error here - "Function lacks ending
 * return statement" - instead of quietly being worded as the other one.
 */
function WarningSentence({ warning }: { readonly warning: AccountingWarning }): ReactElement {
  const quantity = <Money value={money(warning.quantity)} />;

  switch (warning.kind) {
    case 'negative_inventory':
      return (
        <>
          A sale of, or a fee paid in, {warning.asset} exceeded the imported history by {quantity}{' '}
          {warning.asset}. A buy or a deposit is missing.
        </>
      );
    case 'unattributed_fee':
      return (
        <>
          A fee of {quantity} {warning.asset} could not be valued.{' '}
          {warning.charged_to === null
            ? 'It was paid on a conversion between stablecoins.'
            : `It is left out of the figures for ${warning.charged_to}.`}
        </>
      );
  }
}

interface HistoryWarningsProps {
  readonly warnings: readonly AccountingWarning[];
}

/**
 * What the imported history could not account for, and where to look: the moment, the
 * venue, the asset and how much. Collapsed, because it is the detail behind a flag rather
 * than something to read every visit - and it is what makes `history_incomplete` actionable,
 * since the flag alone says an asset's history is short and not where.
 *
 * Carries no trade id: the backend leaves it out of anything that tends to end up in a log,
 * and the moment and the venue identify the fill for the owner.
 */
export function HistoryWarnings({ warnings }: HistoryWarningsProps) {
  return (
    <details className="history-warnings">
      <summary>What the imported history could not account for ({String(warnings.length)})</summary>
      <ul>
        {warnings.map((warning, index) => (
          <li
            key={`${warning.kind}-${warning.occurred_at}-${warning.source}-${warning.asset}-${String(index)}`}
          >
            <AbsoluteTime value={warning.occurred_at} /> on {venueLabel(warning.source)}:{' '}
            <WarningSentence warning={warning} />
          </li>
        ))}
      </ul>
    </details>
  );
}
