import { describeApiError } from '@/api/client';
import { usePortfolioChanges, type Change } from '@/api/changes';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import {
  directionOf,
  formatChange,
  formatPercent,
  PERIOD_LABELS,
  unavailableWords,
} from '@/lib/changes';
import { money } from '@/lib/money';

export const CHANGES_LOADING_LABEL = 'Loading the change over 24 hours and 7 days…';

/** For a first load that got nothing back, as the summary's own failure is worded. */
const LOAD_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const REFETCH_FAILURE_FALLBACK = 'The server could not be reached.';

/** The arrow beside a change: a shape as well as a colour, never the only sign of either. */
const ARROWS = { up: '▲', down: '▼', flat: '' } as const;

/** One period: the change and its percentage, or why there is none. */
function ChangeTile({ change }: { readonly change: Change }) {
  const label = PERIOD_LABELS[change.period];
  const headingId = `change-${change.period}`;

  if (change.change === null) {
    return (
      <div className="change" role="group" aria-labelledby={headingId}>
        <h3 id={headingId} className="kpi-label">
          {label}
        </h3>
        <p className="change-value">—</p>
        <p className="change-note">
          {unavailableWords(change.unavailable ?? 'value_unknown_now', change.period)}
        </p>
      </div>
    );
  }

  const amount = money(change.change);
  const direction = directionOf(amount);

  return (
    <div className={`change change-${direction}`} role="group" aria-labelledby={headingId}>
      <h3 id={headingId} className="kpi-label">
        {label}
      </h3>
      <p className="change-value">
        {direction !== 'flat' && (
          <span className="change-arrow" aria-hidden="true">
            {ARROWS[direction]}
          </span>
        )}
        {formatChange(amount)}
      </p>
      <p className="change-note">
        {change.change_pct === null
          ? 'No percentage: nothing was held then.'
          : formatPercent(money(change.change_pct))}
      </p>
    </div>
  );
}

/**
 * The change widget (spec 041): how much the total moved over the last 24 hours and the last
 * 7 days, each in USDT and as a percentage, green and `+` for a rise, red and `-` for a fall.
 *
 * Its own query and its own four states, like the chart: loading, announced; a failure, in an
 * alert with a retry; and each period's change, or a sentence saying why it is missing. A
 * failed poll after a good load keeps the figures, with a notice.
 */
export function ChangeSummary() {
  const changes = usePortfolioChanges();

  return (
    <section className="card changes-card" aria-labelledby="changes-heading">
      <div className="card-head">
        <div className="history-title">
          <h2 id="changes-heading">Change</h2>
          <span className="page-meta">USDT</span>
        </div>
      </div>
      {changes.isPending ? (
        <Skeleton label={CHANGES_LOADING_LABEL} />
      ) : changes.isLoadingError ? (
        <ErrorState
          title="Could not load the change"
          headingLevel={3}
          description={describeApiError(changes.error, LOAD_FAILURE_FALLBACK)}
          onRetry={() => {
            void changes.refetch();
          }}
        />
      ) : (
        <>
          {changes.isRefetchError && (
            <p className="note note-error" role="alert">
              Could not refresh the change:{' '}
              {describeApiError(changes.error, REFETCH_FAILURE_FALLBACK)} Showing what was last
              loaded.
            </p>
          )}
          <div className="changes-grid">
            {changes.data.changes.map((change) => (
              <ChangeTile key={change.period} change={change} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
