import { useQuery } from '@tanstack/react-query';

import { ApiError, apiFetch } from '@/api/client';

/** Response body of `GET /api/health`. */
interface HealthStatus {
  readonly status: string;
  readonly version: string;
  readonly environment: string;
}

const HEALTH_PATH = '/api/health';

/**
 * Reference page for the whole application: it calls one endpoint and renders
 * each of the three states a query can be in - pending, error and success -
 * explicitly. Every page added later is expected to follow this shape rather
 * than rendering `undefined` data behind an implicit truthiness check.
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
        <p>{describeError(error)}</p>
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
    </section>
  );
}

/** Turns whatever the query failed with into one sentence a user can read. */
function describeError(error: Error): string {
  if (error instanceof ApiError) {
    return error.problem.detail ?? error.problem.title;
  }

  return 'The backend could not be reached. Check that the API is running, then reload the page.';
}
