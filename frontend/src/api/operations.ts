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

/** The page sizes the table offers, and the one it starts on. */
export const OPERATIONS_PAGE_SIZES = [25, 50, 100, 200] as const;
export type OperationsPageSize = (typeof OPERATIONS_PAGE_SIZES)[number];
export const DEFAULT_OPERATIONS_PAGE_SIZE: OperationsPageSize = 50;

export const OPERATIONS_QUERY_KEY_ROOT = 'operations' as const;

/** Under `['portfolio', ...]`, which a balance sync and a wallet change invalidate too. */
export const investmentQueryKey = ['portfolio', 'investment'] as const;

/**
 * What the table asks for: a page of a size, and the filters. A day is a calendar date in this
 * browser's zone, `YYYY-MM-DD` as a date input gives it, and `to` includes its whole day.
 */
export interface OperationsQuery {
  readonly page: number;
  readonly pageSize: OperationsPageSize;
  readonly asset: string;
  readonly venue: string;
  readonly from: string;
  readonly to: string;
}

export function operationsQueryKey(query: OperationsQuery) {
  return [OPERATIONS_QUERY_KEY_ROOT, query] as const;
}

/** Local midnight at the start of `day`, `days` later, as an instant. */
function startOfDay(day: string, days: number): string {
  // A date-time with no offset is read in local time, unlike a bare date, which is UTC.
  const start = new Date(`${day}T00:00`);
  start.setDate(start.getDate() + days);
  return start.toISOString();
}

/** The list's query string: an empty filter is left out rather than sent empty. */
export function operationsSearch(query: OperationsQuery): string {
  const params = new URLSearchParams({
    limit: String(query.pageSize),
    offset: String(query.page * query.pageSize),
  });
  if (query.asset !== '') {
    params.set('asset', query.asset);
  }
  if (query.venue !== '') {
    params.set('venue', query.venue);
  }
  if (query.from !== '') {
    params.set('since', startOfDay(query.from, 0));
  }
  if (query.to !== '') {
    params.set('until', startOfDay(query.to, 1));
  }
  return params.toString();
}

/** One page of the stored operations, newest first. The previous page stays while one loads. */
export function useOperations(query: OperationsQuery): UseQueryResult<OperationList> {
  return useQuery({
    queryKey: operationsQueryKey(query),
    queryFn: ({ signal }) =>
      apiFetch<OperationList>(`${OPERATIONS_PATH}?${operationsSearch(query)}`, { signal }),
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
