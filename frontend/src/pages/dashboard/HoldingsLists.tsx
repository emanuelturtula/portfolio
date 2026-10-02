import { Fragment } from 'react';
import { Link } from 'react-router-dom';

import type { ReconciliationAsset } from '@/api/accounting';
import {
  ALL_QUANTITIES_MATCH,
  HELD_EXCEEDS_HISTORY_GUIDANCE,
  HISTORY_EXCEEDS_HELD_EXPLANATION,
  NOTHING_TO_COMPARE,
  partitionReconciliation,
  RECORD_MISSING_COINS_PROMPT,
} from '@/lib/accounting';
import { adjustmentsRouteFor } from '@/lib/adjustments';
import { ReconciliationTable } from '@/pages/dashboard/ReconciliationTable';

interface HoldingsListProps {
  readonly assets: readonly ReconciliationAsset[];
}

/**
 * The finding: the balances read hold more than the history accounts for. Reading more
 * sources could only widen the gap, so it is stated plainly, in a box of its own - but as a
 * prompt to look, with the thing to rule out first, and not as a verdict: two readings taken
 * at different moments can show the same gap for coins that were only in transit. The weight
 * is in the words and the border; colour is only on top.
 *
 * The way out is a link per asset to the page that records the missing coins, with the
 * asset carried over and nothing else (spec 027).
 */
function HeldExceedsHistory({ assets }: HoldingsListProps) {
  return (
    <div className="holdings-short">
      <h4 id="holdings-short-heading">Held exceeds history ({String(assets.length)})</h4>
      <p>{HELD_EXCEEDS_HISTORY_GUIDANCE}</p>
      <p>
        {RECORD_MISSING_COINS_PROMPT}{' '}
        {assets.map((entry, index) => (
          <Fragment key={entry.asset}>
            {index > 0 && ', '}
            <Link to={adjustmentsRouteFor(entry.asset)}>{entry.asset}</Link>
          </Fragment>
        ))}
      </p>
      <ReconciliationTable assets={assets} labelledBy="holdings-short-heading" />
    </div>
  );
}

/**
 * The quiet list: the history accounts for more than the balances read. Not a finding - the
 * balances are a lower bound on what the owner holds, and several causes this check cannot
 * tell apart put the history above them - so it is closed by default, and its summary says how
 * many without calling them anything.
 */
function HistoryExceedsHeld({ assets }: HoldingsListProps) {
  return (
    <details className="holdings-over">
      <summary id="holdings-over-summary">
        History above the balances read ({String(assets.length)})
      </summary>
      <p>{HISTORY_EXCEEDS_HELD_EXPLANATION}</p>
      <ReconciliationTable assets={assets} labelledBy="holdings-over-summary" />
    </details>
  );
}

/**
 * The comparison itself: the two lists with their different weight, or - when neither has an
 * entry - one line saying why there is nothing to list: every quantity matches, or there was
 * nothing to compare in the first place.
 */
export function AssetComparison({ assets }: HoldingsListProps) {
  const { short, over } = partitionReconciliation(assets);

  return (
    <>
      {short.length > 0 && <HeldExceedsHistory assets={short} />}
      {over.length > 0 && <HistoryExceedsHeld assets={over} />}
      {short.length === 0 && over.length === 0 && (
        <p>{assets.length === 0 ? NOTHING_TO_COMPARE : ALL_QUANTITIES_MATCH}</p>
      )}
    </>
  );
}
