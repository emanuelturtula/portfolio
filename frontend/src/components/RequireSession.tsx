import type { ReactElement } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { describeApiError } from '@/api/client';
import { useSession } from '@/api/session';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';

interface RequireSessionProps {
  readonly children: ReactElement;
}

/**
 * Route guard wrapping one protected element:
 * `<Route path="/" element={<RequireSession><DashboardPage /></RequireSession>} />`.
 *
 * The session query never rejects on `401` (see `useSession`), which
 * collapses this guard to four cases: pending, error, signed-out, signed-in.
 * The signed-out redirect is a declarative `<Navigate>`, never an effect -
 * `react-router-dom` v7 under `StrictMode` double-invokes effects in
 * development, and an effect-driven redirect would fire twice.
 */
export function RequireSession({ children }: RequireSessionProps) {
  const location = useLocation();
  const { data, error, isPending, isError, refetch } = useSession();

  if (isPending) {
    return <Skeleton label="Checking your session…" />;
  }

  if (isError) {
    return (
      <ErrorState
        title="We could not check your session"
        description={describeApiError(
          error,
          'The backend could not be reached. Check that the API is running, then reload the page.',
        )}
        onRetry={() => {
          void refetch();
        }}
      />
    );
  }

  if (data === null) {
    return (
      <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />
    );
  }

  return children;
}
