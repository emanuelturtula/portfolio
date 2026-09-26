import { useEffect, useRef } from 'react';

import { describeApiError } from '@/api/client';
import { useExchanges, useExchangeRuns, useSyncExchanges, type Exchange } from '@/api/exchanges';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { ExchangeList } from '@/pages/exchanges/ExchangeList';
import { SyncResult } from '@/pages/exchanges/SyncResult';
import { SyncRunTable } from '@/pages/exchanges/SyncRunTable';
import { TruncationBanner } from '@/pages/exchanges/TruncationBanner';

const LIST_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const LIST_REFETCH_FALLBACK = 'The server could not be reached.';
const RUNS_UNAVAILABLE_FALLBACK = 'The run log could not be read.';
/** Spec R9: not "the server could not be reached" - a request the coordinator shields from
 * the client connection failing client-side says nothing about whether the server heard it. */
const SYNC_FAILURE_FALLBACK = 'No answer came back from the server.';

const EMPTY_DESCRIPTION =
  'Exchange API keys are read from environment variables on the host, for example ' +
  'PORTFOLIO_BITGET_API_KEY, and are never entered in this app. docs/operations.md, section 12, ' +
  'explains how to create a read-only key and where to put it.';

/**
 * Which venues get a truncation banner (spec criterion 5): `history_truncated` with a known
 * `effective_since`. A type guard rather than a plain predicate, so `TruncationBanner` can
 * declare `effective_since: string` and never re-check a `null` its caller already ruled
 * out - see that component's own doc comment for why a second check would be dead code.
 */
function isTruncated(exchange: Exchange): exchange is Exchange & { effective_since: string } {
  return exchange.history_truncated && exchange.effective_since !== null;
}

/**
 * The exchanges page: account status, a truncation banner per venue whose retention window
 * cut its history short, manual sync progress and result, and the run log. See
 * docs/specs/016-exchanges-page.md.
 *
 * Only the list query failing on its first load is a whole-page failure - the runs query
 * degrades to a notice inside its own section instead, because its absence does not make
 * the account list itself unreadable (same split `DashboardPage` makes between its balances
 * query and its runs query).
 *
 * `syncPending` (this page's own mutation, not any venue's `syncing` flag) is threaded into
 * both queries so a manual sync's progress shows up within one poll interval instead of
 * waiting for a venue's `syncing` flag to be observed first. Per spec 011's "a status must
 * not become a lock", the mutation being pending never disables anything but its own button.
 */
export function ExchangesPage() {
  const syncMutation = useSyncExchanges();
  const syncPending = syncMutation.isPending;
  const exchanges = useExchanges(syncPending);
  // The list's own "is any venue syncing" reading, threaded into the runs query too (spec
  // R12): the runs query is the one place that reads the other query's data, rather than
  // deciding its poll rate from its own alone.
  const anySyncing = exchanges.data?.some((exchange) => exchange.syncing) ?? false;
  const runs = useExchangeRuns(syncPending, anySyncing);

  // What the run log showed at the moment Sync now was last pressed: the newest run's id,
  // and whether that run was itself `running` then (meaning a click would join it rather
  // than start a new one). Never actually read except while `syncMutation.isError` (below),
  // and `handleSyncClick` always writes a fresh value before every `mutate()` - so there is
  // no "not recorded yet" state worth a `null` for a fixture to (never) exercise. Its default
  // is what a click before any run has ever loaded records: `runId: 0, wasRunning: false`.
  const recordedRunRef = useRef<{ readonly runId: number; readonly wasRunning: boolean }>({
    runId: 0,
    wasRunning: false,
  });

  useEffect(() => {
    if (!syncMutation.isError) {
      return;
    }
    const recorded = recordedRunRef.current;
    const newest = runs.data?.[0];
    if (newest === undefined || newest.status === 'running') {
      // Not settled yet - the alert stays exactly as R9 says it should while no new run has
      // appeared: "the request never started one, and the alert stays."
      return;
    }
    const settled =
      newest.run_id > recorded.runId || (newest.run_id === recorded.runId && recorded.wasRunning);
    if (settled) {
      // No need to reset `recordedRunRef` here: `reset()` clears `isError`, so the guard
      // above already short-circuits every later run of this effect until the next click
      // overwrites the ref with a fresh recording anyway.
      syncMutation.reset();
    }
  }, [runs.data, syncMutation]);

  function handleSyncClick(): void {
    // Spec R10: a no-op while pending, not a native `disabled` button - `disabled` drops
    // focus to `<body>` the moment it takes effect, verified in a browser. `aria-disabled`
    // below keeps the button focusable and keeps this handler in charge of the no-op.
    if (syncMutation.isPending) {
      return;
    }
    const newest = runs.data?.[0];
    recordedRunRef.current = {
      runId: newest?.run_id ?? 0,
      wasRunning: newest?.status === 'running',
    };
    syncMutation.mutate();
  }

  if (exchanges.isPending) {
    return <Skeleton label="Loading exchanges…" />;
  }

  // Whole-page failure only when nothing has ever loaded - `isLoadingError`, not `isError`,
  // is what tells the two apart; a background poll failing after a successful load leaves
  // `data` populated with the last good reading (see `exchanges.isError` below).
  if (exchanges.isLoadingError) {
    return (
      <ErrorState
        title="Could not load exchanges"
        description={describeApiError(exchanges.error, LIST_FAILURE_FALLBACK)}
        onRetry={() => {
          void exchanges.refetch();
        }}
      />
    );
  }

  const data = exchanges.data;

  if (data.length === 0) {
    return <EmptyState title="No exchange connected" description={EMPTY_DESCRIPTION} />;
  }

  const anyConfigured = data.some((exchange) => exchange.configured);
  const truncated = data.filter(isTruncated);

  return (
    <section aria-labelledby="exchanges-heading">
      <h2 id="exchanges-heading">Exchanges</h2>

      {exchanges.isError && (
        <p role="alert">
          Could not refresh exchanges: {describeApiError(exchanges.error, LIST_REFETCH_FALLBACK)}{' '}
          Showing what was last loaded.
        </p>
      )}

      {anyConfigured && (
        <div className="exchanges-toolbar">
          <button
            type="button"
            aria-disabled={syncMutation.isPending ? 'true' : undefined}
            onClick={handleSyncClick}
          >
            Sync now
          </button>
          {/* Spec R11: one role="status" element, always in the DOM, whose children swap -
              a region inserted together with its own text is not reliably announced. */}
          <div role="status">
            {syncMutation.isPending &&
              'Syncing exchanges… the first import can take several minutes.'}
            {!syncMutation.isPending && syncMutation.isSuccess && (
              <SyncResult result={syncMutation.data} />
            )}
          </div>
          {!syncMutation.isPending && syncMutation.isError && (
            <p role="alert">
              The sync request failed: {describeApiError(syncMutation.error, SYNC_FAILURE_FALLBACK)}{' '}
              A sync may still be running on the server; this page updates when it finishes.
            </p>
          )}
        </div>
      )}

      {truncated.map((exchange) => (
        <TruncationBanner key={exchange.exchange_key} exchange={exchange} />
      ))}

      <ExchangeList exchanges={data} />

      <section aria-labelledby="sync-history-heading">
        <h3 id="sync-history-heading">Sync history</h3>
        {runs.isPending && <Skeleton label="Loading sync history…" />}
        {runs.isError && (
          <p role="alert">
            Sync history is unavailable: {describeApiError(runs.error, RUNS_UNAVAILABLE_FALLBACK)}{' '}
            Accounts are still shown above.
          </p>
        )}
        {runs.data !== undefined && <SyncRunTable runs={runs.data} />}
      </section>
    </section>
  );
}
