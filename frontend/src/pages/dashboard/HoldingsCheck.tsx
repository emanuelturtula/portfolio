import { useReconciliation, type Reconciliation } from '@/api/accounting';
import { describeApiError } from '@/api/client';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { ErrorState } from '@/components/ErrorState';
import {
  balanceReadings,
  describeComparison,
  failedRecompute,
  HOLDINGS_CHECK_ID,
  missingSources,
} from '@/lib/accounting';
import { BalanceReadings } from '@/pages/dashboard/BalanceReadings';
import { AssetComparison } from '@/pages/dashboard/HoldingsLists';
import { MissingSources } from '@/pages/dashboard/MissingSources';

const LOAD_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const REFETCH_FALLBACK = 'The server could not be reached.';

interface HoldingsCheckBlockProps {
  readonly data: Reconciliation;
  /** The sentence for a refresh that failed while `data` is still the last good reading. */
  readonly refreshFailure: string | undefined;
}

/**
 * The block, once there is a reading: what is compared, which sources are left out of it, the
 * two lists with their different weight, and how old each reading that was compared is. When
 * the last recompute of the history failed, the history may be older than the balances and
 * every asset bought since would look like balances above the history, so one alert replaces
 * the comparison. See docs/specs/025-holdings-reconciliation.md, "Design: frontend".
 */
function HoldingsCheckBlock({ data, refreshFailure }: HoldingsCheckBlockProps) {
  const failed = failedRecompute(data.last_recompute);
  const readings = balanceReadings(data);

  return (
    <section className="card" aria-labelledby={HOLDINGS_CHECK_ID}>
      <h3 id={HOLDINGS_CHECK_ID}>Holdings check</h3>
      <p>{describeComparison(data.tolerance_pct)}</p>

      {refreshFailure !== undefined && (
        <p role="alert">
          Could not refresh the holdings check: {refreshFailure} Showing what was last loaded.
        </p>
      )}
      <MissingSources sources={missingSources(data)} />

      {failed === null ? (
        <AssetComparison assets={data.assets} />
      ) : (
        <p role="alert">
          The last recompute of the history failed on <AbsoluteTime value={failed.at} />, so the
          history may be older than the balances and nothing is compared.
        </p>
      )}

      {readings.length > 0 && <BalanceReadings readings={readings} />}
    </section>
  );
}

/**
 * The holdings check under the invested section: does what the replay says is held agree with
 * the balances actually read? `GET /api/accounting/reconciliation`; the page compares nothing
 * itself. See docs/specs/025-holdings-reconciliation.md.
 *
 * It owns its query and fails on its own, like the section above it:
 *
 * - pending: nothing, since the section already shows one loading region;
 * - failed with nothing to show: an error with a retry;
 * - no snapshot (`computed_at` is `null`): nothing, since the section already says the
 *   positions are not computed - "not computed" is never rendered as "every balance is
 *   unaccounted for";
 * - otherwise the block, and a notice when a later refresh failed and the last reading is
 *   what is on screen.
 */
export function HoldingsCheck() {
  const reconciliation = useReconciliation();

  if (reconciliation.isPending) {
    return null;
  }

  if (reconciliation.isLoadingError) {
    return (
      <ErrorState
        headingLevel={3}
        title="Could not load the holdings check"
        description={describeApiError(reconciliation.error, LOAD_FALLBACK)}
        onRetry={() => {
          void reconciliation.refetch();
        }}
      />
    );
  }

  const data = reconciliation.data;

  if (data.computed_at === null) {
    return null;
  }

  return (
    <HoldingsCheckBlock
      data={data}
      refreshFailure={
        reconciliation.isError
          ? describeApiError(reconciliation.error, REFETCH_FALLBACK)
          : undefined
      }
    />
  );
}
