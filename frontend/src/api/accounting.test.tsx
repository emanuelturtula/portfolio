import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  positionsQueryKey,
  reconciliationQueryKey,
  usePositions,
  useReconciliation,
} from '@/api/accounting';
import { createQueryClient } from '@/lib/queryClient';
import { investedPortfolio, NOW, ZERO } from '@/test/accountingFixtures';
import { fakeAccounting, POSITIONS_PATH, RECONCILIATION_PATH } from '@/test/fakeAccounting';
import {
  assetReconciliation,
  kasNeverTraded,
  matchingReconciliation,
  reconciliation,
  walletReadings,
  type ReconciliationResponse,
} from '@/test/reconciliationFixtures';
import { settle } from '@/test/render';
import { problem, server } from '@/test/server';

afterEach(() => {
  vi.useRealTimers();
});

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe('usePositions', () => {
  it("keys the query under ['accounting'], the prefix an exchange sync invalidates", () => {
    expect(positionsQueryKey).toEqual(['accounting', 'positions']);
  });

  it('reads GET /api/accounting/positions, with no query string, as the server sent it', async () => {
    const fake = fakeAccounting({ positions: investedPortfolio() });
    server.use(...fake.handlers);
    const client = createQueryClient();

    const hook = renderHook(() => usePositions(), { wrapper: wrapperFor(client) });

    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });
    expect(fake.requests.map((entry) => entry.method)).toEqual(['GET']);
    const url = new URL(fake.requests[0]?.url ?? '');
    expect(url.pathname).toBe(POSITIONS_PATH);
    expect(url.search).toBe('');
    // Every money string arrives untouched: 18 places, trailing zeros and all.
    expect(hook.result.current.data).toEqual(investedPortfolio());
    expect(client.getQueryData(positionsQueryKey)).toEqual(investedPortfolio());
  });

  it('polls once a minute, and not sooner', async () => {
    // `Date` and `setInterval` are faked; `setTimeout` stays real, because MSW answers
    // through it. The count is taken once the first answer has landed, since TanStack Query
    // restarts the interval on every query update.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const fake = fakeAccounting();
    server.use(...fake.handlers);
    const hook = renderHook(() => usePositions(), { wrapper: wrapperFor(createQueryClient()) });

    for (let attempt = 0; !hook.result.current.isSuccess && attempt < 40; attempt += 1) {
      await settle();
    }
    expect(hook.result.current.isSuccess).toBe(true);
    await settle();
    const before = fake.count();

    act(() => {
      vi.advanceTimersByTime(59_999);
    });
    await settle();
    expect(fake.count()).toBe(before);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    await settle();
    expect(fake.count()).toBe(before + 1);
  });
});

/**
 * A comparison over the first-time owner's snapshot, which holds nothing: both assets are
 * held and never traded, so their history is zero and the default positions agree with it.
 *
 * - BTC: wallets 0.7, exchanges 0.3, held 1.0, difference +1.0;
 * - ETH: exchanges 3.141592653589793238 - a double holds 3.141592653589793 - difference the same.
 */
function heldAndNeverTraded(): ReconciliationResponse {
  return reconciliation({
    assets: [
      assetReconciliation({
        history_quantity: ZERO,
        difference: '1.000000000000000000',
      }),
      assetReconciliation({
        asset: 'ETH',
        history_quantity: ZERO,
        wallet_quantity: ZERO,
        exchange_quantity: '3.141592653589793238',
        held_quantity: '3.141592653589793238',
        difference: '3.141592653589793238',
      }),
      kasNeverTraded(),
    ],
    wallets: walletReadings(2),
  });
}

describe('useReconciliation', () => {
  it("keys the query under ['accounting'], the prefix an exchange sync invalidates", () => {
    expect(reconciliationQueryKey).toEqual(['accounting', 'reconciliation']);
    // The same prefix as the positions, and an entry of its own beneath it.
    expect(reconciliationQueryKey[0]).toBe(positionsQueryKey[0]);
    expect(reconciliationQueryKey).not.toEqual(positionsQueryKey);
  });

  it('reads GET /api/accounting/reconciliation, with no query string, as the server sent it', async () => {
    const sent = heldAndNeverTraded();
    const fake = fakeAccounting({ reconciliation: sent });
    server.use(...fake.handlers);
    const client = createQueryClient();

    const hook = renderHook(() => useReconciliation(), { wrapper: wrapperFor(client) });

    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });
    expect(fake.requests.map((entry) => entry.method)).toEqual(['GET']);
    const url = new URL(fake.requests[0]?.url ?? '');
    expect(RECONCILIATION_PATH).toBe('/api/accounting/reconciliation');
    expect(url.pathname).toBe(RECONCILIATION_PATH);
    expect(url.search).toBe('');
    // The two reads are separate requests: this hook never asks for the positions.
    expect(fake.count('positions')).toBe(0);
    expect(hook.result.current.data).toEqual(sent);
    expect(client.getQueryData(reconciliationQueryKey)).toEqual(sent);
  });

  it('keeps every quantity a string, to the last of 18 places', async () => {
    const fake = fakeAccounting({ reconciliation: heldAndNeverTraded() });
    server.use(...fake.handlers);

    const hook = renderHook(() => useReconciliation(), {
      wrapper: wrapperFor(createQueryClient()),
    });

    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });
    const [btc, eth] = hook.result.current.data?.assets ?? [];
    // Trailing zeros and all: "1.000000000000000000" is not "1".
    expect(btc?.difference).toBe('1.000000000000000000');
    expect(btc?.history_quantity).toBe(ZERO);
    // The trailing 238 is what a double would lose.
    expect(eth?.exchange_quantity).toBe('3.141592653589793238');
    expect(eth?.difference).toBe('3.141592653589793238');
    expect(hook.result.current.data?.tolerance_pct).toBe('1');
  });

  it('polls once a minute, and not sooner', async () => {
    // As for the positions: `Date` and `setInterval` are faked, `setTimeout` stays real.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const fake = fakeAccounting();
    server.use(...fake.handlers);
    const hook = renderHook(() => useReconciliation(), {
      wrapper: wrapperFor(createQueryClient()),
    });

    for (let attempt = 0; !hook.result.current.isSuccess && attempt < 40; attempt += 1) {
      await settle();
    }
    expect(hook.result.current.isSuccess).toBe(true);
    await settle();
    const before = fake.count('reconciliation');
    expect(before).toBe(1);

    act(() => {
      vi.advanceTimersByTime(59_999);
    });
    await settle();
    expect(fake.count('reconciliation')).toBe(before);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    await settle();
    expect(fake.count('reconciliation')).toBe(before + 1);

    // And again a minute later: a poll, not a single delayed re-read.
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    await settle();
    expect(fake.count('reconciliation')).toBe(before + 2);
    expect(fake.count('positions')).toBe(0);
  });

  it('picks up a new reading on its next poll', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const fake = fakeAccounting();
    server.use(...fake.handlers);
    const hook = renderHook(() => useReconciliation(), {
      wrapper: wrapperFor(createQueryClient()),
    });
    for (let attempt = 0; !hook.result.current.isSuccess && attempt < 40; attempt += 1) {
      await settle();
    }
    expect(hook.result.current.data).toEqual(matchingReconciliation(fake.positions()));

    // A balance read on the server, with no request from the page.
    fake.setReconciliation(heldAndNeverTraded());
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    // Not `waitFor`: with `setInterval` faked and no DOM to observe, it never re-checks.
    await settle();
    await settle();
    expect(hook.result.current.data).toEqual(heldAndNeverTraded());
  });

  it('shares one request between every component that reads it', async () => {
    // The block and the positions table both call the hook: one key, so one request.
    const fake = fakeAccounting();
    server.use(...fake.handlers);

    const hook = renderHook(() => [useReconciliation(), useReconciliation()] as const, {
      wrapper: wrapperFor(createQueryClient()),
    });

    await waitFor(() => {
      expect(hook.result.current[1].isSuccess).toBe(true);
    });
    await settle();
    expect(fake.count('reconciliation')).toBe(1);
    expect(hook.result.current[0].data).toBe(hook.result.current[1].data);
  });

  it('fails on its own, with the sentence the server wrote, and is not retried', async () => {
    const fake = fakeAccounting({ positions: investedPortfolio() });
    fake.fail(
      () => problem(503, 'Service Unavailable', 'The balances table is locked.'),
      'reconciliation',
    );
    server.use(...fake.handlers);

    const hook = renderHook(
      () => ({ positions: usePositions(), reconciliation: useReconciliation() }),
      { wrapper: wrapperFor(createQueryClient()) },
    );

    await waitFor(() => {
      expect(hook.result.current.reconciliation.isLoadingError).toBe(true);
    });
    await waitFor(() => {
      expect(hook.result.current.positions.isSuccess).toBe(true);
    });
    expect(hook.result.current.reconciliation.data).toBeUndefined();
    expect(hook.result.current.reconciliation.error).toMatchObject({
      status: 503,
      message: 'The balances table is locked.',
    });
    // No silent retry hides it: the page is required to show the failure.
    await settle();
    expect(fake.count('reconciliation')).toBe(1);
  });
});
