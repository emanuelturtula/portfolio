import { Link } from 'react-router-dom';

import { useSyncBalances } from '@/api/balances';
import { describeApiError } from '@/api/client';
import { usePortfolioSummary, type PortfolioSummary } from '@/api/portfolio';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { describeMissing } from '@/lib/portfolio';
import { BackupNotice } from '@/pages/dashboard/BackupNotice';
import { ChangeSummary } from '@/pages/dashboard/ChangeSummary';
import { Holdings } from '@/pages/dashboard/Holdings';
import { InvestmentSummary } from '@/pages/dashboard/InvestmentSummary';
import { TotalHero } from '@/pages/dashboard/TotalHero';
import { PortfolioHistory } from '@/pages/dashboard/ValueHistory';

/** See `ValueSection`: a cut-off sync request very often means the run is still going. */
const REFRESH_FAILURE_FALLBACK = 'The server could not be reached.';
const REFETCH_FAILURE_FALLBACK = 'The server could not be reached.';

/** Nothing held and nothing waiting to be read: a new install. */
function isEmpty(summary: PortfolioSummary): boolean {
  return summary.holdings.length === 0 && summary.missing.length === 0;
}

/**
 * The dashboard (spec 039): the total value as the page's hero, then its history, then what is
 * held and how its value splits.
 *
 * Every figure comes from `GET /api/portfolio/summary`, so the page has one query and one set of
 * states. What the total could not include is named in a single line under it, and the total
 * then carries a "Partial" chip; the readings and sync state behind it are on the Wallets page,
 * linked from that line and from the header. The Refresh that re-reads every balance sits in
 * the hero, beside the figure it refreshes, and says there how it went.
 *
 * The change over 24 hours and 7 days (spec 041) and the chart of the value over time (spec
 * 037) each have a query and four states of their own, inside their cards: one that fails to
 * load never takes the figures above it down with it.
 *
 * What was invested and the gain or loss (spec 042) is a card of its own with its own query, after
 * the change.
 *
 * Above everything, a warning when the scheduled backups failed or stopped (spec 029): it is
 * about the data underneath, not about any one figure.
 */
export function DashboardPage() {
  const summary = usePortfolioSummary();
  const sync = useSyncBalances();

  return (
    <div className="page">
      <BackupNotice />
      <Overview summary={summary} sync={sync} />
    </div>
  );
}

interface OverviewProps {
  readonly summary: ReturnType<typeof usePortfolioSummary>;
  readonly sync: ReturnType<typeof useSyncBalances>;
}

function Overview({ summary, sync }: OverviewProps) {
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
        description="Add a wallet to see your portfolio here."
        action={
          <div className="state-actions">
            <Link to="/wallets">Add a wallet</Link>
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
      <TotalHero
        summary={data}
        action={
          <button
            type="button"
            className="button-primary"
            onClick={() => {
              sync.mutate();
            }}
            disabled={sync.isPending}
          >
            Refresh
          </button>
        }
      >
        {data.missing.length > 0 && (
          <p className="note note-warning">
            <strong>Incomplete:</strong> {data.missing.map(describeMissing).join('; ')}.{' '}
            <Link to="/wallets">See wallets</Link>
          </p>
        )}
        {sync.isPending && (
          <p className="note" role="status">
            Reading balances… this can take a minute.
          </p>
        )}
        {sync.isError && (
          <p className="note note-error" role="alert">
            Refresh did not complete: {describeApiError(sync.error, REFRESH_FAILURE_FALLBACK)} A
            sync may still be running on the server; this page updates when it finishes.
          </p>
        )}
      </TotalHero>
      <ChangeSummary />
      <InvestmentSummary />
      <PortfolioHistory />
      {data.holdings.length > 0 && <Holdings summary={data} />}
    </div>
  );
}
