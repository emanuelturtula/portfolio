import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { positionsQueryKey, usePositions } from '@/api/accounting';
import { createQueryClient } from '@/lib/queryClient';
import { investedPortfolio, NOW } from '@/test/accountingFixtures';
import { fakeAccounting, POSITIONS_PATH } from '@/test/fakeAccounting';
import { settle } from '@/test/render';
import { server } from '@/test/server';

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
