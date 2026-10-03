import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { apiFetch, describeApiError } from '@/api/client';
import { useHealthDetail, type BackupStatus } from '@/api/health';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { ErrorState } from '@/components/ErrorState';
import { BACKUP_ERROR_WORDS, BACKUP_STATE_WORDS, UNKNOWN_BACKUP_VALUE } from '@/lib/backups';

/** Response body of `GET /api/health`. */
interface HealthStatus {
  readonly status: string;
  readonly version: string;
  readonly environment: string;
}

const HEALTH_PATH = '/api/health';

const BACKUPS_HEADING_ID = 'backups-heading';

const BACKUPS_UNREACHABLE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';

/**
 * The newest copy: "unknown" while the directory cannot be read (the count is `null` then,
 * and so is the instant), "none yet" for a directory that was read and holds no copy. The two
 * mean opposite things, and neither is shown as the other (spec 029, R4).
 */
function newestBackup(backup: BackupStatus): ReactNode {
  if (backup.count === null) {
    return UNKNOWN_BACKUP_VALUE;
  }

  return backup.latest_at === null ? 'none yet' : <AbsoluteTime value={backup.latest_at} />;
}

/**
 * What the backups section shows once it has an answer, or while it waits for one. See
 * {@link BackupsSection}.
 */
function BackupsContent() {
  const { data, error, isPending, isError } = useHealthDetail();

  if (isPending) {
    return (
      <p className="state" role="status">
        Loading backup status...
      </p>
    );
  }

  if (isError) {
    return (
      <ErrorState
        headingLevel={4}
        title="Could not load the backup status"
        description={describeApiError(error, BACKUPS_UNREACHABLE_FALLBACK)}
      />
    );
  }

  const { backup } = data;

  return (
    <dl className="health-details">
      <dt>State</dt>
      <dd>{BACKUP_STATE_WORDS[backup.state]}</dd>
      <dt>Newest backup</dt>
      <dd>{newestBackup(backup)}</dd>
      <dt>Backups kept</dt>
      <dd>{backup.count ?? UNKNOWN_BACKUP_VALUE}</dd>
      {backup.last_error_kind !== null && (
        <>
          <dt>Last failure</dt>
          <dd>{BACKUP_ERROR_WORDS[backup.last_error_kind]}</dd>
        </>
      )}
    </dl>
  );
}

/**
 * The scheduled backups, from `GET /api/health/detail`. See docs/specs/029-sqlite-backups.md.
 *
 * Its own query, so it renders the same three states as the page around it and fails on its
 * own: the liveness check answering says nothing about whether this endpoint did. The
 * heading stays through all three, so the section does not appear from nowhere.
 *
 * - success: the state in words, the newest copy's date and time - or "none yet", an honest
 *   absence that the state beside it tells apart from a failure, or "unknown" when the
 *   directory could not be read - and how many copies there are. A failed attempt also
 *   names why, in words;
 * - a request that failed is an error, never a state: nothing here is shown as `ok`, or as
 *   zero copies, on the strength of a request that did not answer.
 */
function BackupsSection() {
  return (
    <section aria-labelledby={BACKUPS_HEADING_ID}>
      <h3 id={BACKUPS_HEADING_ID}>Backups</h3>
      <BackupsContent />
    </section>
  );
}

/**
 * Reference page for the whole application: it calls one endpoint and renders
 * each of the three states a query can be in - pending, error and success -
 * explicitly. Every page added later is expected to follow this shape rather
 * than rendering `undefined` data behind an implicit truthiness check.
 *
 * The backups section under the details is a second query with the same three states of
 * its own, mounted only once the health check has answered: a backend that is not up has
 * already been reported above, and a second loading line and a second alert for the same
 * outage would only repeat it.
 */
export function HealthPage() {
  const { data, error, isPending, isError } = useQuery({
    queryKey: ['health'],
    queryFn: ({ signal }) => apiFetch<HealthStatus>(HEALTH_PATH, { signal }),
  });

  if (isPending) {
    // `role="status"` is an ARIA live region, so screen readers announce the
    // wait instead of the user facing silence while a spinner turns.
    return (
      <p className="state" role="status">
        Loading backend health...
      </p>
    );
  }

  if (isError) {
    return (
      <div className="state state-error" role="alert">
        <h2>The backend health check failed</h2>
        <p>
          {describeApiError(
            error,
            'The backend could not be reached. Check that the API is running, then reload the page.',
          )}
        </p>
      </div>
    );
  }

  return (
    <section aria-labelledby="health-heading">
      <h2 id="health-heading">Backend health</h2>
      <dl className="health-details">
        <dt>Status</dt>
        <dd>{data.status}</dd>
        <dt>Version</dt>
        <dd>{data.version}</dd>
        <dt>Environment</dt>
        <dd>{data.environment}</dd>
      </dl>
      <BackupsSection />
    </section>
  );
}
