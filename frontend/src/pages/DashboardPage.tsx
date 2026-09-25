import { Link } from 'react-router-dom';

import { useCurrentBalances, useSyncBalances, useSyncRuns } from '@/api/balances';
import { describeApiError } from '@/api/client';
import { useWallets } from '@/api/wallets';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { RelativeTime } from '@/components/RelativeTime';
import { Skeleton } from '@/components/Skeleton';
import { NEVER_SYNCED_MESSAGE, selectSettledRun, type SyncRunSummary } from '@/lib/freshness';
import { AssetTable } from '@/pages/dashboard/AssetTable';
import { TotalSummary } from '@/pages/dashboard/TotalSummary';
import { WalletBalanceTable } from '@/pages/dashboard/WalletBalanceTable';

// `settled.status` is typed over every `SyncRunStatus`, `'running'` included, even though
// `selectSettledRun` never actually returns a running run as `settled` - the coordinator's
// "at most one running run" invariant lives at the value level, not in this type. A
// `'running'` entry keeps this total without asserting away a case `tsc` cannot rule out.
const SETTLED_RUN_VERBS: Record<SyncRunSummary['status'], string> = {
  running: 'is running',
  success: 'succeeded',
  partial: 'partially succeeded',
  failed: 'failed',
  interrupted: 'was interrupted',
};

/**
 * The portfolio value dashboard: total, per-asset and per-wallet value, a refresh button
 * and a "last updated" indicator. See docs/specs/011-wallets-page-value-dashboard.md.
 *
 * Only the current-balances query failing is a whole-page failure - the runs query and the
 * wallets query each degrade to a notice instead, because neither one's absence makes the
 * balances themselves unreadable, only less complete a picture.
 */
export function DashboardPage() {
  const balances = useCurrentBalances();
  const runs = useSyncRuns();
  const wallets = useWallets(false);
  const syncMutation = useSyncBalances();

  if (balances.isPending) {
    return <Skeleton label="Loading your portfolio…" />;
  }

  if (balances.isError) {
    return (
      <ErrorState
        title="Could not load your portfolio"
        description={describeApiError(
          balances.error,
          'The backend could not be reached. Check that the API is running, then reload the page.',
        )}
        onRetry={() => {
          void balances.refetch();
        }}
      />
    );
  }

  const data = balances.data;

  if (data.wallets.length === 0) {
    return (
      <EmptyState
        title="No wallets yet"
        description="Add a wallet to start tracking its balance and value."
        action={<Link to="/wallets">Add a wallet</Link>}
      />
    );
  }

  const freshnessKnown = runs.isSuccess;
  const { settled, inProgress } = freshnessKnown
    ? selectSettledRun(runs.data)
    : { settled: undefined, inProgress: false };
  const walletsById = new Map((wallets.data ?? []).map((wallet) => [wallet.id, wallet]));

  return (
    <div className="dashboard">
      {wallets.isError && (
        <p role="alert">
          Addresses are unavailable:{' '}
          {describeApiError(wallets.error, 'the wallet list could not be read')}. Wallet rows show
          their label, or chain and id, instead.
        </p>
      )}
      {runs.isError && (
        <p role="alert">
          Sync status is unavailable:{' '}
          {describeApiError(runs.error, 'the run log could not be read')}. Balances are still shown
          below.
        </p>
      )}

      <div className="dashboard-toolbar">
        <button
          type="button"
          onClick={() => {
            syncMutation.mutate();
          }}
          disabled={syncMutation.isPending || inProgress}
        >
          Refresh
        </button>
        {inProgress && <p role="status">A sync is running…</p>}
        <p className="last-updated">
          Balances as of {data.as_of === null ? 'never' : <RelativeTime value={data.as_of} />}.{' '}
          {freshnessKnown &&
            (settled === undefined ? (
              NEVER_SYNCED_MESSAGE
            ) : (
              <>
                Last sync {SETTLED_RUN_VERBS[settled.status]}{' '}
                <RelativeTime value={settled.finished_at ?? settled.started_at} />.
              </>
            ))}
        </p>
        {syncMutation.isError && (
          <p role="alert">
            Refresh failed:{' '}
            {describeApiError(syncMutation.error, 'Could not reach the server to start a sync.')}
          </p>
        )}
      </div>

      <TotalSummary data={data} />
      <AssetTable data={data} />
      <WalletBalanceTable
        data={data}
        walletsById={walletsById}
        settledRun={settled}
        freshnessKnown={freshnessKnown}
      />
    </div>
  );
}
