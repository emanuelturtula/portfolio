import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { healthDetailQueryKey, useHealthDetail } from '@/api/health';
import { createQueryClient } from '@/lib/queryClient';
import { failedBackup, healthDetail, serveBackup } from '@/test/backupFixtures';
import { settle } from '@/test/render';
import { server } from '@/test/server';

/** One minute, as the other read-only endpoints poll. */
const POLL_MS = 60_000;

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('useHealthDetail', () => {
  it('reads GET /api/health/detail under its own key beside the liveness check', async () => {
    const served = serveBackup(failedBackup);
    server.use(served.handler);
    const client = createQueryClient();

    const hook = renderHook(() => useHealthDetail(), { wrapper: wrapperFor(client) });

    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });
    expect(hook.result.current.data).toEqual(healthDetail(failedBackup));
    expect(healthDetailQueryKey).toEqual(['health', 'detail']);
    expect(client.getQueryData(['health', 'detail'])).toEqual(healthDetail(failedBackup));
    expect(served.requests()).toBe(1);
  });

  it('asks again every minute, so a failure overnight reaches a page left open', async () => {
    /*
     * `setInterval` is faked and `setTimeout` is not: MSW answers through the real one, and
     * TanStack Query schedules `refetchInterval` with `setInterval`.
     */
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const served = serveBackup(failedBackup);
    server.use(served.handler);
    const hook = renderHook(() => useHealthDetail(), {
      wrapper: wrapperFor(createQueryClient()),
    });
    for (let attempt = 0; attempt < 40 && !hook.result.current.isSuccess; attempt += 1) {
      await settle();
    }
    expect(served.requests()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(POLL_MS - 1);
    });
    await settle();
    expect(served.requests()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    await settle();
    expect(served.requests()).toBe(2);
  });
});
