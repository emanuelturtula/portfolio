import { useState } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import type { Exchange, ExchangeRun } from '@/api/exchanges';
import { RelativeTime } from '@/components/RelativeTime';
import { Skeleton } from '@/components/Skeleton';
import { RUN_STATUS_LABELS, syncHistoryNeedsAttention } from '@/lib/exchanges';
import { SyncRunTable } from '@/pages/exchanges/SyncRunTable';

const RUNS_UNAVAILABLE_FALLBACK = 'The run log could not be read.';

interface SyncHistoryDisclosureProps {
  readonly runs: UseQueryResult<ExchangeRun[]>;
  /** `undefined` is an exchange list that could not be read. */
  readonly exchanges: readonly Exchange[] | undefined;
}

/**
 * The run log, demoted to a disclosure: the owner cares about what was bought and sold, not
 * about the log, so it is loud only when something is wrong.
 *
 * - **The summary always shows the newest run's outcome and age**, open or closed.
 * - **It starts open** when the newest run is `partial`, `failed` or `interrupted`, or an
 *   account is `error` or `auth_failed`, and closed otherwise.
 * - **Once the owner toggles it, their choice wins** for the rest of the page's life: a poll
 *   that finds a new failure does not reopen a log they closed.
 *
 * The `<details>` is controlled: the summary's click is taken over (`preventDefault`) and
 * writes the state, from the element's own `open`, and the element itself. The native `toggle` event is deliberately
 * not used, because it also fires when `open` changes for any other reason, and a log that
 * opened itself because a run failed would be recorded as the owner's own choice.
 *
 * The heading stays outside the `<details>`, so the section keeps its place in the outline,
 * and the disclosure does not exist until there is a run to summarise. A pending or failed
 * run log is a status or an alert, as it always was, and Accounts are shown either way.
 */
export function SyncHistoryDisclosure({ runs, exchanges }: SyncHistoryDisclosureProps) {
  const [ownerChoice, setOwnerChoice] = useState<boolean | null>(null);
  const newest = runs.data?.[0];
  const open = ownerChoice ?? syncHistoryNeedsAttention(newest, exchanges);

  return (
    <section aria-labelledby="sync-history-heading">
      <h3 id="sync-history-heading">Sync history</h3>
      {runs.isPending && <Skeleton label="Loading sync history…" />}
      {runs.isError && (
        <p role="alert">
          Sync history is unavailable: {describeApiError(runs.error, RUNS_UNAVAILABLE_FALLBACK)}{' '}
          Accounts are still shown above.
        </p>
      )}
      {runs.data !== undefined &&
        (newest === undefined ? (
          <SyncRunTable runs={runs.data} />
        ) : (
          <details open={open}>
            <summary
              onClick={(event) => {
                // Take over the native toggle, and read the element's own state to do it: the
                // browser can open a <details> without React (find-in-page does), and `open`
                // here is only what React last rendered. Working from that would "toggle" to
                // the state the element is already in, and the click would seem to do nothing.
                // The element is written to directly as well as the state, because React does
                // not touch an attribute whose rendered value has not changed.
                event.preventDefault();
                const details = event.currentTarget.parentElement as HTMLDetailsElement;
                const next = !details.open;
                details.open = next;
                setOwnerChoice(next);
              }}
            >
              Latest run: {RUN_STATUS_LABELS[newest.status]}, started{' '}
              <RelativeTime value={newest.started_at} />
            </summary>
            <SyncRunTable runs={runs.data} />
          </details>
        ))}
    </section>
  );
}
