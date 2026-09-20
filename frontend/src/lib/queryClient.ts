import { QueryClient } from '@tanstack/react-query';

/**
 * Builds the TanStack Query client used by the application and by tests.
 *
 * Tests use the very same factory, so the retry and refetch behaviour they
 * exercise is the behaviour that ships.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
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
}
