/**
 * Fetchers and hooks for spec 042: `/api/exchange-operations` (upload, list, manual entries)
 * and `GET /api/investment`.
 *
 * Types come from the generated schema and nowhere else. Every quantity and amount is a
 * string, and a figure nothing could work out is `null` with its reason, never `"0"`.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { apiFetch, apiSend } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type Operation = components['schemas']['OperationResponse'];
export type OperationList = components['schemas']['OperationListResponse'];
export type ImportReport = components['schemas']['ImportResponse'];
export type ImportFileReport = components['schemas']['ImportFileResponse'];
export type ManualOperationRequest = components['schemas']['ManualOperationRequest'];
export type Investment = components['schemas']['InvestmentResponse'];
export type AssetInvestment = components['schemas']['AssetInvestmentResponse'];
export type TotalInvestment = components['schemas']['TotalInvestmentResponse'];
export type InvestmentUnavailable = components['schemas']['InvestmentUnavailable'];

export const OPERATIONS_PATH = '/api/exchange-operations';
export const IMPORTS_PATH = '/api/exchange-operations/imports';
export const INVESTMENT_PATH = '/api/investment';

/** Rows per page of the operations table. */
export const OPERATIONS_PAGE_SIZE = 50;

export const OPERATIONS_QUERY_KEY_ROOT = 'operations' as const;

/** Under `['portfolio', ...]`, which a balance sync and a wallet change invalidate too. */
export const investmentQueryKey = ['portfolio', 'investment'] as const;

export function operationsQueryKey(page: number) {
  return [OPERATIONS_QUERY_KEY_ROOT, { page }] as const;
}

/** One page of the stored operations, newest first. The previous page stays while one loads. */
export function useOperations(page: number): UseQueryResult<OperationList> {
  return useQuery({
    queryKey: operationsQueryKey(page),
    queryFn: ({ signal }) =>
      apiFetch<OperationList>(
        `${OPERATIONS_PATH}?limit=${String(OPERATIONS_PAGE_SIZE)}&offset=${String(page * OPERATIONS_PAGE_SIZE)}`,
        { signal },
      ),
    placeholderData: keepPreviousData,
  });
}

export function useInvestment(): UseQueryResult<Investment> {
  return useQuery({
    queryKey: investmentQueryKey,
    queryFn: ({ signal }) => apiFetch<Investment>(INVESTMENT_PATH, { signal }),
  });
}

/** Every write changes the table and the invested figures alike. */
async function invalidateOperations(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: [OPERATIONS_QUERY_KEY_ROOT] }),
    queryClient.invalidateQueries({ queryKey: investmentQueryKey }),
  ]);
}

/** A file's bytes in base64, as the upload carries them (spec 042, R12). */
export async function fileToBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const chunk = 0x8000;
  let binary = '';
  for (let start = 0; start < bytes.length; start += chunk) {
    binary += String.fromCharCode(...bytes.subarray(start, start + chunk));
  }
  return btoa(binary);
}

export function useImportOperations(): UseMutationResult<ImportReport, unknown, File> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (file: File) =>
      apiFetch<ImportReport>(IMPORTS_PATH, {
        method: 'POST',
        body: { filename: file.name, content_base64: await fileToBase64(file) },
      }),
    onSuccess: () => invalidateOperations(queryClient),
  });
}

export function useCreateManualOperation(): UseMutationResult<
  Operation,
  unknown,
  ManualOperationRequest
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ManualOperationRequest) =>
      apiFetch<Operation>(OPERATIONS_PATH, { method: 'POST', body }),
    onSuccess: () => invalidateOperations(queryClient),
  });
}

export function useDeleteOperation(): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiSend(`${OPERATIONS_PATH}/${String(id)}`, { method: 'DELETE' }),
    onSuccess: () => invalidateOperations(queryClient),
  });
}
