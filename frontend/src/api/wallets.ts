/**
 * Fetchers and hooks for the wallet registry: `GET/POST /api/wallets`,
 * `PATCH/DELETE /api/wallets/{id}`.
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

import { apiFetch, apiSend } from '@/api/client';
import type { components } from '@/api/generated/schema';
import { usePortfolioScope, withPortfolio } from '@/lib/portfolioScope';

export type Wallet = components['schemas']['WalletResponse'];
export type ChainKey = components['schemas']['ChainKey'];
export type CreateWalletRequest = components['schemas']['WalletCreateRequest'];

const WALLETS_PATH = '/api/wallets';

/** Query key root every wallet mutation invalidates by, per the spec's query table. */
export const WALLETS_QUERY_KEY_ROOT = 'wallets' as const;

export function walletsQueryKey(includeArchived: boolean, scope: number | null = null) {
  return [WALLETS_QUERY_KEY_ROOT, { includeArchived, scope }] as const;
}

async function fetchWallets(
  includeArchived: boolean,
  scope: number | null,
  signal: AbortSignal,
): Promise<Wallet[]> {
  const query = includeArchived ? '?include_archived=true' : '';
  const response = await apiFetch<components['schemas']['WalletListResponse']>(
    withPortfolio(`${WALLETS_PATH}${query}`, scope),
    { signal },
  );
  return response.wallets;
}

/**
 * The caller's wallets. `includeArchived: true` returns active *and* archived wallets
 * together - the backend does not offer "archived only" - so the list page marks each
 * archived row in text rather than filtering by request.
 */
export function useWallets(includeArchived: boolean): UseQueryResult<Wallet[]> {
  const { scope } = usePortfolioScope();
  return useQuery({
    queryKey: walletsQueryKey(includeArchived, scope),
    queryFn: ({ signal }) => fetchWallets(includeArchived, scope, signal),
  });
}

/**
 * Both balance query keys, invalidated by every wallet mutation: a new, archived or
 * restored wallet changes what the dashboard shows next, per the spec's mutation table.
 */
async function invalidateWalletsAndBalances(queryClient: QueryClient): Promise<void> {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: [WALLETS_QUERY_KEY_ROOT] }),
    queryClient.invalidateQueries({ queryKey: ['balances'] }),
    queryClient.invalidateQueries({ queryKey: ['portfolio'] }),
    // A portfolio's count of wallets (spec 044).
    queryClient.invalidateQueries({ queryKey: ['portfolios'] }),
  ]);
}

export function useCreateWallet(): UseMutationResult<Wallet, unknown, CreateWalletRequest> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateWalletRequest) =>
      apiFetch<Wallet>(WALLETS_PATH, { method: 'POST', body }),
    onSuccess: () => invalidateWalletsAndBalances(queryClient),
  });
}

export interface WalletMutationOptions {
  /**
   * Runs after the wallet and balance queries have been invalidated and awaited - as the
   * hook's own `onSuccess`, not a callback passed to a particular `.mutate()` call.
   *
   * That distinction is why this exists: a callback passed to `mutate(vars, {onSuccess})`
   * is dropped by TanStack Query once the observer that issued it is gone - which for a
   * per-row mutation means "once that row unmounts" - while the hook-level `onSuccess`
   * configured here keeps running regardless, because it belongs to the mutation itself
   * rather than to whichever component happened to call `.mutate()`. A row that archives
   * itself out of the active-only view needs exactly that: the caller still needs to know
   * once the invalidated data has landed, even after its own row is gone.
   */
  readonly onSuccess?: () => void;
}

export function useArchiveWallet(
  options: WalletMutationOptions = {},
): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (walletId: number) =>
      apiSend(`${WALLETS_PATH}/${String(walletId)}`, { method: 'DELETE' }),
    onSuccess: async () => {
      await invalidateWalletsAndBalances(queryClient);
      options.onSuccess?.();
    },
  });
}

export function useRestoreWallet(
  options: WalletMutationOptions = {},
): UseMutationResult<Wallet, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (walletId: number) =>
      apiFetch<Wallet>(`${WALLETS_PATH}/${String(walletId)}`, {
        method: 'PATCH',
        body: { archived: false },
      }),
    onSuccess: async () => {
      await invalidateWalletsAndBalances(queryClient);
      options.onSuccess?.();
    },
  });
}

/**
 * Moves a wallet, with its whole history, to another portfolio (spec 044, R6). Every figure can
 * change, so every query is invalidated, as for the other wallet mutations.
 */
export function useMoveWallet(): UseMutationResult<
  Wallet,
  unknown,
  { readonly walletId: number; readonly portfolioId: number }
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ walletId, portfolioId }) =>
      apiFetch<Wallet>(`${WALLETS_PATH}/${String(walletId)}`, {
        method: 'PATCH',
        body: { portfolio_id: portfolioId },
      }),
    onSuccess: () =>
      Promise.all([
        invalidateWalletsAndBalances(queryClient),
        queryClient.invalidateQueries({ queryKey: ['operations'] }),
      ]),
  });
}
