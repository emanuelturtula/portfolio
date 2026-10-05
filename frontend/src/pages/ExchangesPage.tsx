import { useEffect, useRef } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import {
  exchangeFillsQueryKey,
  exchangeRunsQueryKey,
  useExchanges,
  useExchangeRuns,
  useSyncExchanges,
} from '@/api/exchanges';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { venuesWithFailedSync } from '@/lib/accounting';
import { isTruncated } from '@/lib/exchanges';
import { ExchangeList } from '@/pages/exchanges/ExchangeList';
import { FailingAccountAlerts } from '@/pages/exchanges/FailingAccountAlerts';
import { SyncHistoryDisclosure } from '@/pages/exchanges/SyncHistoryDisclosure';
import { SyncResult } from '@/pages/exchanges/SyncResult';
import { TransactionsSection } from '@/pages/exchanges/TransactionsSection';
import { TruncationBanner } from '@/pages/exchanges/TruncationBanner';

const LIST_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const LIST_REFETCH_FALLBACK = 'The server could not be reached.';
/** Spec R9: not "the server could not be reached" - a request the coordinator shields from
 * the client connection failing client-side says nothing about whether the server heard it. */
const SYNC_FAILURE_FALLBACK = 'No answer came back from the server.';

const EMPTY_DESCRIPTION =
  'Exchange API keys are read from environment variables on the host, for example ' +
  'PORTFOLIO_BITGET_API_KEY, and are never entered in this app. docs/operations.md, ' +
  'sections 12 (Bitget) and 14 (BingX) explain how to create a read-only key and where to ' +
  'put it.';

/**
 * The exchanges page: the imported transactions with their filters and totals, account
 * status, a truncation banner per venue whose retention window cut its history short, manual
 * sync progress and result, and the run log. See docs/specs/016-exchanges-page.md and
 * docs/specs/024-exchange-transactions.md.
 *
 * **Each data source fails on its own** (spec 024). The exchange list failing on its first
 * load is an error inside Accounts, with the toolbar hidden because `configured` is unknown,
 * and Transactions still render, saying that completeness is unknown. The fills request
 * failing is an error inside Transactions, and Accounts still render. The runs query degrades
 * to a notice inside Sync history, as it always did. The one thing that replaces the page is
 * a list that loaded and is empty: there is nothing to import from, so Transactions have no
 * reason to render.
 *
 * `syncPending` (this page's own mutation, not any venue's `syncing` flag) is threaded into
 * both queries so a manual sync's progress shows up within one poll interval instead of
 * waiting for a venue's `syncing` flag to be observed first. Per spec 011's "a status must
 * not become a lock", the mutation being pending never disables anything but its own button.
 */
export function ExchangesPage() {
  const queryClient = useQueryClient();
  const syncMutation = useSyncExchanges();
  const syncPending = syncMutation.isPending;
  const exchanges = useExchanges(syncPending);
  // The list's own "is any venue syncing" reading, threaded into the runs query too (spec
  // R12): the runs query is the one place that reads the other query's data, rather than
  // deciding its poll rate from its own alone.
  const anySyncing = exchanges.data?.some((exchange) => exchange.syncing) ?? false;
  const runs = useExchangeRuns(syncPending, anySyncing);
  const newestRun = runs.data?.[0];
  /**
   * Whether a manual sync is actually retrying `auth_failed` accounts right now (spec R17),
   * from the run log rather than from `syncPending`: a `POST` that *joined* a scheduled run
   * is pending too, and that run still skips them. `false` while the run log is unknown.
   */
  const manualRunInFlight = newestRun?.status === 'running' && newestRun.trigger === 'manual';

  /**
   * Spec R15: the list and the run log poll on independent 5 s phases, so when the list's
   * poll lands first after a run ends, the run log can keep reading `running` for up to a
   * minute at the slow rate before its own next poll - the accounts would say "Up to date"
   * beside a "Running" run log entry. On the falling edge of `anySyncing` (true to false),
   * the run log is invalidated once instead of waited out.
   *
   * Guarded on the run log's *own* newest run still reading `running`: when the two queries
   * happen to be in phase and the run log already caught up in the same tick that flipped
   * `anySyncing`, invalidating again would only fetch a second time for data already fresh.
   */
  const previousAnySyncingRef = useRef(anySyncing);
  useEffect(() => {
    if (previousAnySyncingRef.current && !anySyncing && newestRun?.status === 'running') {
      void queryClient.invalidateQueries({ queryKey: exchangeRunsQueryKey });
    }
    previousAnySyncingRef.current = anySyncing;
  }, [anySyncing, newestRun, queryClient]);

  /**
   * Spec 024, R5/S1: the transactions are refreshed when the stored fills change, not only when
   * this page's own sync settles. A *scheduled* sync can recover a failing venue and store
   * fills: the next list poll clears the failing alert and the completeness sentence, and
   * without this the table and its totals would keep the old count with no warning at all.
   *
   * The signal is the list's per-venue `fills_stored`, which the list already polls, so this
   * adds no request of its own. It is compared only while no venue is `syncing`: during a
   * backfill every committed page raises the count, and refreshing the whole fills view and
   * its totals at each 5 s poll for minutes would be polling by another name. The list shows
   * `pending_windows` and the completeness notice says the import is unfinished meanwhile, so
   * the owner is not misled while it runs. The baseline is not advanced during that time, so
   * the first settled reading after the run still differs from the one before it, and a run
   * that starts and ends between two slow polls is seen the same way.
   *
   * The first reading is only recorded: the fills query has just been asked for it. After a
   * manual sync the fills are invalidated twice (by the mutation, then here), which is one
   * spare request.
   */
  const fillsSignature = exchanges.data
    ?.map((exchange) => `${exchange.exchange_key}:${String(exchange.fills_stored)}`)
    .join(',');
  const fillsBaselineRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    const baseline = fillsBaselineRef.current;
    if (baseline === undefined) {
      fillsBaselineRef.current = fillsSignature;
      return;
    }
    if (anySyncing) {
      return;
    }
    fillsBaselineRef.current = fillsSignature;
    if (fillsSignature !== baseline) {
      void queryClient.invalidateQueries({ queryKey: exchangeFillsQueryKey });
    }
  }, [anySyncing, fillsSignature, queryClient]);

  /**
   * What the run log showed at the moment Sync now was last pressed: the newest run's id,
   * and whether that run was itself `running` then (meaning a click would join it rather
   * than start a new one). `runId` is `null` when the run log's own state was unknown at
   * that moment - still loading, or its first load had already failed (spec R18) - which is
   * also the one baseline the effect below never auto-clears from: otherwise a run log that
   * later recovers showing some unrelated old run could be mistaken for evidence this
   * request settled. Only a later click, once the run log is known, replaces it.
   */
  const recordedRunRef = useRef<{ readonly runId: number | null; readonly wasRunning: boolean }>({
    runId: 0,
    wasRunning: false,
  });

  // Spec R19: the effect below depends on exactly these two, not the whole `syncMutation` -
  // a fresh object every render. `reset` is stable across renders (`MutationObserver`'s
  // constructor binds it once - `this.reset = this.reset.bind(this)` in
  // `@tanstack/query-core`), so naming it here satisfies `exhaustive-deps` with no
  // suppression needed.
  const { isError: syncFailed, reset: resetSync } = syncMutation;

  useEffect(() => {
    if (!syncFailed) {
      return;
    }
    const recorded = recordedRunRef.current;
    if (recorded.runId === null) {
      return;
    }
    // Recomputed from `runs.data` (already a dependency) rather than closing over the
    // render-scoped `newestRun` above, so this effect's own dependency list - spec R19 -
    // can state exactly what it reads instead of a broader, always-fresh mutation object.
    const newest = runs.data?.[0];
    if (newest === undefined || newest.status === 'running') {
      // Not settled yet - the alert stays exactly as R9 says it should while no new run has
      // appeared: "the request never started one, and the alert stays."
      return;
    }
    // Spec R18: a later *scheduled* run reaching a higher id is not evidence this request
    // ever ran, so the id-greater branch also requires `trigger: 'manual'`. The equal-id
    // branch needs no such check - it means this request joined the very run recorded as
    // `running` at the click, whatever that run's own trigger was.
    const settled =
      (newest.run_id > recorded.runId && newest.trigger === 'manual') ||
      (newest.run_id === recorded.runId && recorded.wasRunning);
    if (settled) {
      // No need to reset `recordedRunRef` here: `reset()` clears `isError`, so the guard
      // above already short-circuits every later run of this effect until the next click
      // overwrites the ref with a fresh recording anyway.
      resetSync();
    }
  }, [runs.data, syncFailed, resetSync]);

  function handleSyncClick(): void {
    // Spec R10: a no-op while pending, not a native `disabled` button - `disabled` drops
    // focus to `<body>` the moment it takes effect, verified in a browser. `aria-disabled`
    // below keeps the button focusable and keeps this handler in charge of the no-op.
    if (syncMutation.isPending) {
      return;
    }
    recordedRunRef.current = {
      runId: runs.data === undefined ? null : (newestRun?.run_id ?? 0),
      wasRunning: newestRun?.status === 'running',
    };
    syncMutation.mutate();
  }

  if (exchanges.isPending) {
    return <Skeleton label="Loading exchanges…" />;
  }

  // `undefined` only when the list has never loaded: a background poll failing after a
  // successful load leaves `data` populated with the last good reading (`isRefetchError`).
  const data = exchanges.data;

  if (data?.length === 0) {
    return <EmptyState title="No exchange connected" description={EMPTY_DESCRIPTION} />;
  }

  const anyConfigured = data?.some((exchange) => exchange.configured) === true;
  const truncated = data?.filter(isTruncated) ?? [];
  const failing = data === undefined ? [] : venuesWithFailedSync(data);

  return (
    <section className="page" aria-labelledby="exchanges-heading">
      <div className="page-head">
        <h2 id="exchanges-heading" className="page-title">
          Exchanges
        </h2>
        {anyConfigured && (
          <button
            type="button"
            className="button-primary"
            aria-disabled={syncMutation.isPending ? 'true' : undefined}
            onClick={handleSyncClick}
          >
            Sync now
          </button>
        )}
      </div>

      {exchanges.isRefetchError && (
        <p role="alert">
          Could not refresh exchanges: {describeApiError(exchanges.error, LIST_REFETCH_FALLBACK)}{' '}
          Showing what was last loaded.
        </p>
      )}

      {anyConfigured && (
        <div className="exchanges-toolbar">
          {/* Spec R11: one role="status" element, always in the DOM, whose children swap -
              a region inserted together with its own text is not reliably announced. Spec R14
              kept Sync now from stretching to the toolbar; it now sits in the page head. */}
          <div role="status" className="note">
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

      {/* Accounts sit below the transactions now, so a failing one is named here, above them. */}
      <FailingAccountAlerts venues={failing} />

      <TransactionsSection exchanges={data} />

      {truncated.map((exchange) => (
        <TruncationBanner key={exchange.exchange_key} exchange={exchange} />
      ))}

      {exchanges.isLoadingError ? (
        <section className="stack" aria-labelledby="exchanges-accounts-heading">
          <h3 id="exchanges-accounts-heading" className="section-title">
            Accounts
          </h3>
          <ErrorState
            headingLevel={4}
            title="Could not load exchanges"
            description={describeApiError(exchanges.error, LIST_FAILURE_FALLBACK)}
            onRetry={() => {
              void exchanges.refetch();
            }}
          />
        </section>
      ) : (
        <ExchangeList exchanges={exchanges.data} manualRunInFlight={manualRunInFlight} />
      )}

      <SyncHistoryDisclosure runs={runs} exchanges={data} />
    </section>
  );
}
