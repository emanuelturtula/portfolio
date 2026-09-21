import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query';

import { ApiError } from '@/api/client';
import { sessionQueryKey } from '@/api/session';

/**
 * Builds the TanStack Query client used by the application and by tests.
 *
 * Tests use the very same factory, so the retry and refetch behaviour they
 * exercise is the behaviour that ships.
 *
 * A session can die mid-use - the idle window expires, the row is revoked
 * elsewhere - and any query or mutation can be the one to discover it. The
 * `QueryCache`/`MutationCache` `onError` below is the single place that
 * reacts: on any `401`, it writes `null` into the session query's cache
 * entry, then drops every other cached query - everything else was fetched
 * under the session that just died, so none of it is trustworthy for
 * whoever signs in next in this tab. `RequireSession` is already watching
 * the session entry, so the redirect to `/login` happens through the same
 * path as a cold start, and no component anywhere else has to know the rule
 * exists.
 *
 * `createQueryClient` builds the client inside a closure over a `let` so the
 * cache callbacks can reference the very client they are configured on -
 * the documented TanStack pattern for this shape of rule.
 */
export function createQueryClient(): QueryClient {
  // `handleError` closes over `client` before it exists; the client is
  // assigned exactly once, but `const` cannot be declared without an
  // initializer, so the assignment has to come after this declaration.
  // eslint-disable-next-line prefer-const
  let client: QueryClient;

  function handleError(error: unknown): void {
    if (error instanceof ApiError && error.status === 401) {
      // Order matters: the session entry is set first, on the query instance
      // the guard is already watching, so its redirect fires immediately.
      // `clear()` destroys and lazily recreates cache entries, and an
      // already-mounted observer does not pick up a recreated instance in
      // time for this render - clearing the session key along with
      // everything else would turn an immediate redirect into one waiting on
      // a refetch. Everything other than the session key is then dropped, so
      // stale data from the dead session cannot outlive it.
      client.setQueryData(sessionQueryKey, null);
      client.removeQueries({ predicate: (query) => query.queryKey[0] !== sessionQueryKey[0] });
    }
  }

  client = new QueryClient({
    queryCache: new QueryCache({ onError: handleError }),
    mutationCache: new MutationCache({ onError: handleError }),
    defaultOptions: {
      queries: {
        // The skeleton has a single endpoint and every page must render its
        // error state honestly; silent retries would hide it.
        retry: false,
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
    },
  });

  return client;
}
