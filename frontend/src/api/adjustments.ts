/**
 * Fetchers and hooks for the manual adjustments: `GET/POST /api/accounting/adjustments`,
 * `PUT/DELETE /api/accounting/adjustments/{id}`, and `GET /api/accounting/first-trades`, which
 * the form reads to suggest a date. See docs/specs/027-manual-adjustments-page.md, and
 * docs/specs/023-manual-adjustments.md for what an adjustment is.
 *
 * Types come from the generated schema and nowhere else, per CLAUDE.md rule "Types come
 * from the backend."
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';

import { ApiError, apiFetch, apiSend } from '@/api/client';
import type { components } from '@/api/generated/schema';

export type Adjustment = components['schemas']['AdjustmentResponse'];
export type AdjustmentCreateRequest = components['schemas']['AdjustmentCreateRequest'];
export type AdjustmentReplaceRequest = components['schemas']['AdjustmentReplaceRequest'];
export type FirstTrades = components['schemas']['FirstTradesResponse'];
export type FirstTrade = components['schemas']['FirstTradeResponse'];

const ADJUSTMENTS_PATH = '/api/accounting/adjustments';
const FIRST_TRADES_PATH = '/api/accounting/first-trades';

/**
 * Both queries sit under `['accounting']`, the prefix an exchange sync invalidates and every
 * adjustment mutation invalidates: a change to an adjustment moves the positions, the
 * holdings check, the list, and nothing of that is worth showing from before it.
 */
export const adjustmentsQueryKey = ['accounting', 'adjustments'] as const;
export const firstTradesQueryKey = ['accounting', 'first-trades'] as const;

/** The owner's adjustments in the order they replay in: by `occurred_at`, then id. */
export function useAdjustments(): UseQueryResult<Adjustment[]> {
  return useQuery({
    queryKey: adjustmentsQueryKey,
    queryFn: async ({ signal }) => {
      const response = await apiFetch<components['schemas']['AdjustmentListResponse']>(
        ADJUSTMENTS_PATH,
        { signal },
      );
      return response.adjustments;
    },
  });
}

/**
 * When the imported history of each asset begins. The form uses it for its date suggestion
 * and its list of assets, and works without it: no part of saving depends on this query.
 */
export function useFirstTrades(): UseQueryResult<FirstTrades> {
  return useQuery({
    queryKey: firstTradesQueryKey,
    queryFn: ({ signal }) => apiFetch<FirstTrades>(FIRST_TRADES_PATH, { signal }),
  });
}

/**
 * Every adjustment mutation invalidates the whole `['accounting']` root, which covers the
 * positions, the holdings check, the list and the first trades. Returned, so the mutation
 * stays pending until the queries mounted on the page have read the change.
 */
function invalidateAccounting(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: ['accounting'] });
}

/**
 * Whether the API answered that the adjustment is not there: deleted in another tab, or from
 * the console. Any other failure says nothing about whether it exists.
 */
function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function useCreateAdjustment(): UseMutationResult<
  Adjustment,
  unknown,
  AdjustmentCreateRequest
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: AdjustmentCreateRequest) =>
      apiFetch<Adjustment>(ADJUSTMENTS_PATH, { method: 'POST', body }),
    onSuccess: () => invalidateAccounting(queryClient),
  });
}

export interface ReplaceAdjustmentVariables {
  readonly id: number;
  /** All five fields: `PUT` replaces the adjustment, `unit_cost` included. */
  readonly body: AdjustmentReplaceRequest;
}

/**
 * Replaces an adjustment's five fields. A `404` means it was deleted elsewhere, so the list on
 * screen is out of date and is read again; the row the owner is editing stops being there.
 * The form shows the API's sentence, and this is only the part that is about the cache.
 */
export function useReplaceAdjustment(): UseMutationResult<
  Adjustment,
  unknown,
  ReplaceAdjustmentVariables
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, body }: ReplaceAdjustmentVariables) =>
      apiFetch<Adjustment>(`${ADJUSTMENTS_PATH}/${String(id)}`, { method: 'PUT', body }),
    onSuccess: () => invalidateAccounting(queryClient),
    onError: (error) => {
      if (isNotFound(error)) {
        void queryClient.invalidateQueries({ queryKey: adjustmentsQueryKey });
      }
    },
  });
}

/** How a delete ended with the adjustment gone: by this request, or before it (a `404`). */
export type DeleteOutcome = 'deleted' | 'already_deleted';

/**
 * Deletes an adjustment, and tells `onGone` how it came to be gone.
 *
 * `onGone` is the hook's own, not a callback passed to a particular `.mutate()` call, for the
 * reason `useArchiveWallet` gives: TanStack Query drops a per-call callback once the observer
 * that issued it is gone, and the row that issued this one is exactly what the delete removes
 * before the request has finished settling.
 *
 * **A `404` is an outcome of the delete, not a failure of it.** The adjustment is already gone,
 * which is what the owner asked for, so it takes the same road as a success with its own
 * outcome: `['accounting']` is invalidated whole - the delete made elsewhere moved the
 * positions too - and awaited, then `onGone('already_deleted')` runs. Returning the promise
 * keeps the mutation pending until the list has been read again, so the row that offered the
 * delete is gone before it could show an alert about it. Any other failure is a failure: the
 * mutation errors, the row stays and says why, and nothing is read again.
 */
export function useDeleteAdjustment(
  onGone: (outcome: DeleteOutcome) => void,
): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => apiSend(`${ADJUSTMENTS_PATH}/${String(id)}`, { method: 'DELETE' }),
    onSuccess: async () => {
      await invalidateAccounting(queryClient);
      onGone('deleted');
    },
    onError: async (error) => {
      if (isNotFound(error)) {
        await invalidateAccounting(queryClient);
        onGone('already_deleted');
      }
    },
  });
}
