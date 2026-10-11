/**
 * Fetchers and hooks for spec 044: `/api/portfolios` (list, create, rename, delete) and
 * `POST /api/exchange-operations/move`.
 *
 * Types come from the generated schema and nowhere else. `wallets` and `operations` on a
 * portfolio are counts, not money.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { apiFetch, apiSend } from '@/api/client';
import type { components } from '@/api/generated/schema';
import { useSession } from '@/api/session';

export type Portfolio = components['schemas']['PortfolioResponse'];
export type PortfolioList = components['schemas']['PortfolioListResponse'];
export type MoveOperationsRequest = components['schemas']['MoveOperationsRequest'];
export type MoveOperationsResult = components['schemas']['MoveOperationsResponse'];

export const PORTFOLIOS_PATH = '/api/portfolios';
export const MOVE_OPERATIONS_PATH = '/api/exchange-operations/move';

export const portfoliosQueryKey = ['portfolios'] as const;

/**
 * Every portfolio, oldest first. Read only with a session: the selector asks for it on every
 * page, the login page included, and a signed-out visitor has no portfolios to list.
 */
export function usePortfolios(): UseQueryResult<Portfolio[]> {
  const session = useSession();
  return useQuery({
    queryKey: portfoliosQueryKey,
    queryFn: async ({ signal }) =>
      (await apiFetch<PortfolioList>(PORTFOLIOS_PATH, { signal })).portfolios,
    enabled: Boolean(session.data),
  });
}

/**
 * Everything a move or a portfolio change can alter: the list and its counts, and every
 * figure, which is read per portfolio.
 */
export async function invalidateEverything(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: portfoliosQueryKey }),
    queryClient.invalidateQueries({ queryKey: ['wallets'] }),
    queryClient.invalidateQueries({ queryKey: ['balances'] }),
    queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
    queryClient.invalidateQueries({ queryKey: ['operations'] }),
  ]);
}

export function useCreatePortfolio(): UseMutationResult<Portfolio, unknown, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<Portfolio>(PORTFOLIOS_PATH, { method: 'POST', body: { name } }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: portfoliosQueryKey }),
  });
}

export function useRenamePortfolio(): UseMutationResult<
  Portfolio,
  unknown,
  { readonly id: number; readonly name: string }
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, name }) =>
      apiFetch<Portfolio>(`${PORTFOLIOS_PATH}/${String(id)}`, {
        method: 'PATCH',
        body: { name },
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: portfoliosQueryKey }),
  });
}

export function useDeletePortfolio(): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiSend(`${PORTFOLIOS_PATH}/${String(id)}`, { method: 'DELETE' }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: portfoliosQueryKey }),
  });
}

export function useMoveOperations(): UseMutationResult<
  MoveOperationsResult,
  unknown,
  MoveOperationsRequest
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MoveOperationsRequest) =>
      apiFetch<MoveOperationsResult>(MOVE_OPERATIONS_PATH, { method: 'POST', body }),
    onSuccess: () => invalidateEverything(queryClient),
  });
}
