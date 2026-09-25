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
 * Fallback shown when `POST /api/balances/sync` fails with no real problem document - a
 * network error, or a proxy's own error page for a request the coordinator held open for
 * the whole run (see the spec's Risks section). Paired with a fixed trailing sentence that
 * deliberately does not say the sync did not start: the coordinator shields the run from
 * the client connection, so a cut-off request very often means the run is still going, not
 * that it never began. A real `problem.detail` from the backend still wins over this
 * fallback - see `describeApiError` - but the trailing sentence is shown either way, since
 * even a definite backend-side answer does not rule out the run continuing past it.
 */
const REFRESH_FAILURE_FALLBACK = 'The server could not be reached.';

/**
 * Fallbacks for the two degrade-to-notice cases, each a full sentence: every real
 * `problem.detail` from the backend already is one, and concatenating our own trailing
 * sentence straight after it - with no literal "." of our own in between - would otherwise
 * print two full stops back to back for every genuine server refusal, not just a synthetic
 * test case. See `describeApiError`: a fallback is used only when there is no real detail
 * to defer to, so it has to carry its own punctuation.
 */
const WALLETS_UNAVAILABLE_FALLBACK = 'The wallet list could not be read.';
const RUNS_UNAVAILABLE_FALLBACK = 'The run log could not be read.';

/**
 * Fallback for a background poll of `GET /api/balances/current` that fails after the page
 * already has data - a container restart mid-poll is routine (every deploy does one), and
 * with `data` still on hand there is nothing to fall back to blank for. See `isRefetchError`
 * below: TanStack Query keeps the last successful `data` across a subsequent failed fetch,
 * which is what lets this stay a notice instead of losing the page.
 */
const BALANCES_REFETCH_FALLBACK = 'The server could not be reached.';

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

  // Whole-page only when there is nothing to show at all - the first load failed, or every
  // load has. `isLoadingError` (as opposed to `isError`) is what tells the two apart: a
  // background poll that fails *after* a successful load leaves `data` populated with the
  // last good reading (see `isRefetchError` below), and blanking a loaded dashboard because
  // one poll missed is worse than leaving it stale - the container restarts on every deploy.
  if (balances.isLoadingError) {
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

  const runsKnown = runs.isSuccess;
  const { settled, inProgress, runningRun } = runsKnown
    ? selectSettledRun(runs.data)
    : { settled: undefined, inProgress: false, runningRun: undefined };
  // Stricter than `runsKnown`, and it is what goes to the per-row and per-total judgments
  // below, not the "last sync" line above: while `balances.isRefetchError`, `data` is still
  // showing the *previous* reading, but `runs` has already moved on to whatever the newest
  // run is. Judging that stale reading against a newer run's `started_at` makes every row
  // read "not covered" and the total claim balances the newest sync could not refresh -
  // both false, since the reading on screen simply has not been re-fetched, not skipped by
  // anything. The run log itself is still accurate regardless, which is why "last sync
  // succeeded 5 minutes ago" keeps showing off `runsKnown` alone.
  const freshnessKnown = runsKnown && !balances.isRefetchError;
  const walletsById = new Map((wallets.data ?? []).map((wallet) => [wallet.id, wallet]));

  return (
    <div className="dashboard">
      {balances.isError && (
        <p role="alert">
          Could not refresh the portfolio:{' '}
          {describeApiError(balances.error, BALANCES_REFETCH_FALLBACK)} Showing what was last
          loaded.
        </p>
      )}
      {wallets.isError && (
        <p role="alert">
          Addresses are unavailable: {describeApiError(wallets.error, WALLETS_UNAVAILABLE_FALLBACK)}{' '}
          Wallet rows show their label, or chain and id, instead.
        </p>
      )}
      {runs.isError && (
        <p role="alert">
          Sync status is unavailable: {describeApiError(runs.error, RUNS_UNAVAILABLE_FALLBACK)}{' '}
          Balances are still shown below.
        </p>
      )}

      <div className="dashboard-toolbar">
        <button
          type="button"
          onClick={() => {
            syncMutation.mutate();
          }}
          disabled={syncMutation.isPending}
        >
          Refresh
        </button>
        {syncMutation.isPending && (
          <p role="status">Refreshing balances… this can take a minute.</p>
        )}
        {inProgress && runningRun !== undefined && (
          // Not a live region: `<RelativeTime>` ticks every 30s, and a `role="status"` here
          // would re-announce "started N minutes ago" on every tick. This is page state to
          // read on demand, not a transition the owner triggered - unlike the refresh-pending
          // line above, which is, and keeps its `role="status"`.
          <p>
            A sync started <RelativeTime value={runningRun.started_at} /> and has not finished.
          </p>
        )}
        <p className="last-updated">
          Balances as of {data.as_of === null ? 'never' : <RelativeTime value={data.as_of} />}.{' '}
          {runsKnown &&
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
            Refresh did not complete:{' '}
            {describeApiError(syncMutation.error, REFRESH_FAILURE_FALLBACK)} A sync may still be
            running on the server; this page updates when it finishes.
          </p>
        )}
      </div>

      <TotalSummary data={data} settledRun={settled} freshnessKnown={freshnessKnown} />
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
