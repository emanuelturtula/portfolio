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
 * The manual sync's result summary (spec criterion 3), shown in a `role="status"` block
 * that stays on screen until the next sync - `ExchangesPage` swaps this out for the pending
 * line the moment a new sync is fired, which is what "stays until the next sync" means in
 * practice.
 */
export function SyncResult({ result }: SyncResultProps) {
  const duration = formatRunDuration(result.duration_ms);
  const fillsWord = result.fills_inserted === 1 ? 'fill' : 'fills';
  const exchangeWord = result.accounts_total === 1 ? 'exchange' : 'exchanges';

  const failed = result.accounts.filter((account) => account.status === 'failed');
  const skipped = result.accounts.filter((account) => account.status === 'skipped');

  return (
    <div role="status">
      <p>
        The sync {RUN_VERBS[result.status]}: {formatCount(result.fills_inserted)} new {fillsWord} (
        {formatCount(result.fills_seen)} read) from {formatCount(result.accounts_total)}{' '}
        {exchangeWord}, in {duration}.
      </p>
      {result.joined && <p>It joined a sync that was already running.</p>}
      {failed.map((account) => {
        const venue = EXCHANGES[account.exchange_key].name;
        return (
          <p key={account.exchange_key}>
            {venue}: {accountFailureSentence(account.error_kind, venue)}
          </p>
        );
      })}
      {skipped.map((account) => {
        const venue = EXCHANGES[account.exchange_key].name;
        return (
          <p key={account.exchange_key}>
            {venue} was skipped, because its key was refused earlier and only a sync you start
            retries it. Press Sync now again to retry it.
          </p>
        );
      })}
    </div>
  );
}
