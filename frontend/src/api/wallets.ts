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

export type Wallet = components['schemas']['WalletResponse'];
export type ChainKey = components['schemas']['ChainKey'];
export type CreateWalletRequest = components['schemas']['WalletCreateRequest'];

const WALLETS_PATH = '/api/wallets';

/** Query key root every wallet mutation invalidates by, per the spec's query table. */
export const WALLETS_QUERY_KEY_ROOT = 'wallets' as const;

export function walletsQueryKey(includeArchived: boolean) {
  return [WALLETS_QUERY_KEY_ROOT, { includeArchived }] as const;
}

async function fetchWallets(includeArchived: boolean, signal: AbortSignal): Promise<Wallet[]> {
  const query = includeArchived ? '?include_archived=true' : '';
  const response = await apiFetch<components['schemas']['WalletListResponse']>(
    `${WALLETS_PATH}${query}`,
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
  return useQuery({
    queryKey: walletsQueryKey(includeArchived),
    queryFn: ({ signal }) => fetchWallets(includeArchived, signal),
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

export function useArchiveWallet(): UseMutationResult<void, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (walletId: number) =>
      apiSend(`${WALLETS_PATH}/${String(walletId)}`, { method: 'DELETE' }),
    onSuccess: () => invalidateWalletsAndBalances(queryClient),
  });
}

export function useRestoreWallet(): UseMutationResult<Wallet, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (walletId: number) =>
      apiFetch<Wallet>(`${WALLETS_PATH}/${String(walletId)}`, {
        method: 'PATCH',
        body: { archived: false },
      }),
    onSuccess: () => invalidateWalletsAndBalances(queryClient),
  });
}
