import { Link } from 'react-router-dom';

import { useSyncBalances } from '@/api/balances';
import { describeApiError } from '@/api/client';
import { usePortfolioSummary, type PortfolioSummary } from '@/api/portfolio';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { isZeroMoney, money } from '@/lib/money';
import { describeMissing } from '@/lib/portfolio';
import { BackupNotice } from '@/pages/dashboard/BackupNotice';
import { Holdings } from '@/pages/dashboard/Holdings';
import { SummaryCards } from '@/pages/dashboard/SummaryCards';

/** See `ValueSection`: a cut-off sync request very often means the run is still going. */
const REFRESH_FAILURE_FALLBACK = 'The server could not be reached.';
const REFETCH_FAILURE_FALLBACK = 'The server could not be reached.';

/** Nothing held, nothing invested, nothing waiting to be read: a new install. */
function isEmpty(summary: PortfolioSummary): boolean {
  return (
    summary.holdings.length === 0 &&
    summary.untracked.length === 0 &&
    summary.missing.length === 0 &&
    isZeroMoney(money(summary.invested))
  );
}

/**
 * The dashboard (#154): three figures, then what is held and how its value splits.
 *
 * Every figure comes from `GET /api/portfolio/summary`, so the page has one query and one set of
 * states. What a figure could not include is named in a single line under the figures, and each
 * figure it touches carries a "Partial" chip; the readings, sync state and per-position detail
 * behind them are on the Details page, linked from that line and from the header.
 *
 * Above everything, a warning when the scheduled backups failed or stopped (spec 029): it is
 * about the data underneath, not about any one figure.
 */
export function DashboardPage() {
  const summary = usePortfolioSummary();
  const sync = useSyncBalances();

  return (
    <>
      <BackupNotice />
      <div className="page-head">
        <button
          type="button"
          onClick={() => {
            sync.mutate();
          }}
          disabled={sync.isPending}
        >
          Refresh
        </button>
      </div>
      {sync.isPending && (
        <p className="note" role="status">
          Reading balances… this can take a minute.
        </p>
      )}
      {sync.isError && (
        <p className="note note-error" role="alert">
          Refresh did not complete: {describeApiError(sync.error, REFRESH_FAILURE_FALLBACK)} A sync
          may still be running on the server; this page updates when it finishes.
        </p>
      )}
      <Overview summary={summary} />
    </>
  );
}

function Overview({ summary }: { readonly summary: ReturnType<typeof usePortfolioSummary> }) {
  if (summary.isPending) {
    return <Skeleton label="Loading your portfolio…" />;
  }

  // A failed poll after a good load keeps the figures on screen, with a notice: the container
  // restarts on every deploy, and blanking the dashboard for one missed poll would be worse
  // than showing it a minute old.
  if (summary.isLoadingError) {
    return (
      <ErrorState
        title="Could not load your portfolio"
        description={describeApiError(
          summary.error,
          'The backend could not be reached. Check that the API is running, then reload the page.',
        )}
        onRetry={() => {
          void summary.refetch();
        }}
      />
    );
  }

  const data = summary.data;

  if (isEmpty(data)) {
    return (
      <EmptyState
        title="Nothing to show yet"
        description="Add a wallet or connect an exchange to see your portfolio here."
        action={
          <div className="state-actions">
            <Link to="/wallets">Add a wallet</Link>
            <Link to="/exchanges">Connect an exchange</Link>
          </div>
        }
      />
    );
  }

  return (
    <div className="overview">
      {summary.isRefetchError && (
        <p className="note note-error" role="alert">
          Could not refresh: {describeApiError(summary.error, REFETCH_FAILURE_FALLBACK)} Showing
          what was last loaded.
        </p>
      )}
      <SummaryCards summary={data} />
      {data.missing.length > 0 && (
        <p className="note note-warning">
          <strong>Incomplete:</strong> {data.missing.map(describeMissing).join('; ')}.{' '}
          <Link to="/details">See details</Link>
        </p>
      )}
      {(data.holdings.length > 0 || data.untracked.length > 0) && <Holdings summary={data} />}
    </div>
  );
}
