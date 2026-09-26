import type { ExchangeSyncTriggered } from '@/api/exchanges';
import {
  accountFailureSentence,
  EXCHANGES,
  formatCount,
  formatRunDuration,
  type SyncRunStatus,
} from '@/lib/exchanges';

/**
 * Verbs for the summary sentence's "The sync {verb}:" opener - not `RUN_STATUS_LABELS`,
 * which is Title Case for the run-log table column and does not read as a verb for every
 * member (`running` there is "Running", not "is running"). Mirrors `DashboardPage`'s own
 * `SETTLED_RUN_VERBS` for the balance sync, for the same reason.
 */
const RUN_VERBS: Record<SyncRunStatus, string> = {
  running: 'is running',
  success: 'succeeded',
  partial: 'partially succeeded',
  failed: 'failed',
  interrupted: 'was interrupted',
};

interface SyncResultProps {
  readonly result: ExchangeSyncTriggered;
}

/**
 * The manual sync's result summary (spec criterion 3). No `role="status"` of its own - spec
 * R11 moved that to the one persistent element `ExchangesPage`'s toolbar holds, so this only
 * ever supplies that element's children, swapped in once the request settles successfully.
 *
 * **R8: the headline counts what was actually attempted.** `accounts_skipped` never reached
 * the venue at all (a scheduled or startup run skipping an `auth_failed` account, joined by
 * this request), so `attempted = accounts_total - accounts_skipped` is what "from N exchanges"
 * should count, and "No exchange was read." replaces the whole sentence when that is 0 -
 * rather than a true "0 exchanges" reading nobody was actually asked. When any account was
 * skipped, the skip lines and the joined line both lead the headline instead of following it:
 * the reason the read was incomplete belongs before the summary of what it still achieved.
 */
export function SyncResult({ result }: SyncResultProps) {
  const duration = formatRunDuration(result.duration_ms);
  const fillsWord = result.fills_inserted === 1 ? 'fill' : 'fills';
  const attempted = result.accounts_total - result.accounts_skipped;
  const exchangeWord = attempted === 1 ? 'exchange' : 'exchanges';

  const failed = result.accounts.filter((account) => account.status === 'failed');
  const skipped = result.accounts.filter((account) => account.status === 'skipped');

  const headline = (
    <p>
      {attempted === 0
        ? 'No exchange was read.'
        : `The sync ${RUN_VERBS[result.status]}: ${formatCount(result.fills_inserted)} new ` +
          `${fillsWord} (${formatCount(result.fills_seen)} read) from ${formatCount(attempted)} ` +
          `${exchangeWord}, in ${duration}.`}
    </p>
  );
  const joinedLine = result.joined && <p>It joined a sync that was already running.</p>;

  const failedLines = failed.map((account) => {
    const venue = EXCHANGES[account.exchange_key].name;
    return (
      <p key={account.exchange_key}>
        {venue}: {accountFailureSentence(account.error_kind, venue)}
      </p>
    );
  });
  const skippedLines = skipped.map((account) => {
    const venue = EXCHANGES[account.exchange_key].name;
    return (
      <p key={account.exchange_key}>
        {venue} was skipped, because its key was refused earlier and only a sync you start retries
        it. Press Sync now again to retry it.
      </p>
    );
  });

  return (
    <>
      {skipped.length > 0 ? (
        <>
          {skippedLines}
          {joinedLine}
          {headline}
        </>
      ) : (
        <>
          {headline}
          {joinedLine}
        </>
      )}
      {failedLines}
    </>
  );
}
