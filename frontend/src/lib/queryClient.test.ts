import { QueryClientProvider, useMutation, type QueryClient } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { createElement, type ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { apiFetch, apiSend } from '@/api/client';
import { sessionQueryKey } from '@/api/session';
import { createQueryClient } from '@/lib/queryClient';
import { problem, server, TEST_USERNAME, unauthorized } from '@/test/server';

const PROTECTED_PATH = '/api/wallets';

/** A client that already believes it holds a live session. */
function signedInClient(): QueryClient {
  const client = createQueryClient();
  client.setQueryData(sessionQueryKey, { username: TEST_USERNAME });
  return client;
}

async function captureRejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error: unknown) {
    return error;
  }

  throw new Error('Expected the request to reject, but it resolved.');
}

describe('createQueryClient', () => {
  it('invalidates the cached session when any query fails with 401', async () => {
    server.use(http.get(PROTECTED_PATH, () => unauthorized()));
    const client = signedInClient();

    await captureRejection(
      client.query({ queryKey: ['wallets'], queryFn: () => apiFetch(PROTECTED_PATH) }),
    );

    // Not `undefined`, which would leave the session query refetching and the
    // guard showing a skeleton; `null`, which the guard already reads as
    // "signed out" and redirects on. One rule, in one place, and nothing calls
    // `navigate` from outside the router.
    expect(client.getQueryData(sessionQueryKey)).toBeNull();
  });

  it('invalidates the cached session when a mutation fails with 401', async () => {
    server.use(http.post(PROTECTED_PATH, () => unauthorized()));
    const client = signedInClient();

    const { result } = renderHook(
      () => useMutation({ mutationFn: () => apiSend(PROTECTED_PATH, { method: 'POST' }) }),
      {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(QueryClientProvider, { client }, children),
      },
    );

    result.current.mutate();

    await waitFor(() => {
      expect(client.getQueryData(sessionQueryKey)).toBeNull();
    });
  });

  it('leaves the cached session alone when a query fails for any other reason', async () => {
    server.use(
      http.get(PROTECTED_PATH, () =>
        problem(500, 'Internal Server Error', 'The server encountered an unexpected condition.'),
      ),
    );
    const client = signedInClient();

    await captureRejection(
      client.query({ queryKey: ['wallets'], queryFn: () => apiFetch(PROTECTED_PATH) }),
    );

    // The absence assertion. A rule that clears the session on every failure
    // signs the owner out whenever the Pi hiccups, and passes a presence-only
    // test while doing it.
    expect(client.getQueryData(sessionQueryKey)).toEqual({ username: TEST_USERNAME });
  });

  it('leaves the cached session alone when a query succeeds', async () => {
    server.use(http.get(PROTECTED_PATH, () => HttpResponse.json([])));
    const client = signedInClient();

    await client.query({ queryKey: ['wallets'], queryFn: () => apiFetch(PROTECTED_PATH) });

    expect(client.getQueryData(sessionQueryKey)).toEqual({ username: TEST_USERNAME });
  });

  it('does not retry, so a failing page shows its error state honestly', async () => {
    let calls = 0;
    server.use(
      http.get(PROTECTED_PATH, () => {
        calls += 1;
        return problem(500, 'Internal Server Error', 'Nope.');
      }),
    );
    const client = createQueryClient();

    await captureRejection(
      client.query({ queryKey: ['wallets'], queryFn: () => apiFetch(PROTECTED_PATH) }),
    );

    expect(calls).toBe(1);
  });
});
