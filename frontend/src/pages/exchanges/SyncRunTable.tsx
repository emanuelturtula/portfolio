import type { ExchangeRun } from '@/api/exchanges';
import { RelativeTime } from '@/components/RelativeTime';
import {
  accountFailureSentence,
  EXCHANGES,
  formatCount,
  formatRunDuration,
  OUTCOME_LABELS,
  RUN_STATUS_LABELS,
  TRIGGER_LABELS,
} from '@/lib/exchanges';

/**
 * The Exchanges column. A settled run (`success`, `partial` or `failed`) reads its three
 * closing counters: "{n} succeeded, {n} failed, {n} skipped", zero counts left out. A
 * `running` or `interrupted` run has not closed, so those counters are still 0 - only
 * `finish_run` writes them - and this instead counts the outcomes actually recorded so far
 * against `accounts_total`, which doubles as a progress readout while the run is in flight.
 * "None" wins over either shape when the run attempted nothing at all.
 */
function exchangesCell(run: ExchangeRun): string {
  if (run.accounts_total === 0) {
    return 'None';
  }

  if (run.status === 'running' || run.status === 'interrupted') {
    return `${formatCount(run.accounts.length)} of ${formatCount(run.accounts_total)} finished`;
  }

  const parts: string[] = [];
  if (run.accounts_succeeded > 0) {
    parts.push(`${formatCount(run.accounts_succeeded)} succeeded`);
  }
  if (run.accounts_failed > 0) {
    parts.push(`${formatCount(run.accounts_failed)} failed`);
  }
  if (run.accounts_skipped > 0) {
    parts.push(`${formatCount(run.accounts_skipped)} skipped`);
  }
  return parts.join(', ');
}

/**
 * The Details column: one line per account, "{Venue}: {outcome label}", a failed one
 * followed by its sentence and the backend-redacted `detail`.
 *
 * `detail` is rendered as a React text node only, per the spec's "'Redacted errors' is the
 * backend's guarantee, and the frontend's job is not to undo it" - never
 * `dangerouslySetInnerHTML`, never a link, never placed in a URL, never logged.
 */
function DetailsCell({ run }: { readonly run: ExchangeRun }) {
  return (
    <ul>
      {run.accounts.map((account) => {
        const venue = EXCHANGES[account.exchange_key].name;
        return (
          <li key={account.exchange_key}>
            {venue}: {OUTCOME_LABELS[account.status]}
            {account.status === 'failed' && (
              <>
                {' '}
                {accountFailureSentence(account.error_kind, venue)}
                {account.detail !== null && <> Detail: {account.detail}</>}
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}

interface SyncRunTableProps {
  readonly runs: readonly ExchangeRun[];
}

/**
 * The run log (spec criterion 4): the newest runs first, each with its trigger, status,
 * duration, account and fill counts and per-account detail. An empty log renders a fixed
 * sentence instead of an empty table.
 */
export function SyncRunTable({ runs }: SyncRunTableProps) {
  if (runs.length === 0) {
    return <p>No exchange sync has run yet.</p>;
  }

  return (
    <table>
      <thead>
        <tr>
          <th scope="col">Started</th>
          <th scope="col">Trigger</th>
          <th scope="col">Status</th>
          <th scope="col">Duration</th>
          <th scope="col">Exchanges</th>
          <th scope="col">Fills</th>
          <th scope="col">Details</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => (
          <tr key={run.run_id}>
            <td>
              <RelativeTime value={run.started_at} />
            </td>
            <td>{TRIGGER_LABELS[run.trigger]}</td>
            <td>{RUN_STATUS_LABELS[run.status]}</td>
            <td>{formatRunDuration(run.duration_ms)}</td>
            <td>{exchangesCell(run)}</td>
            <td>
              {formatCount(run.fills_inserted)} new of {formatCount(run.fills_seen)} read
            </td>
            <td>
              <DetailsCell run={run} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
