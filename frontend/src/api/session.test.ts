import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { createElement, type ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { ApiError } from '@/api/client';
import { login, logout, useSession } from '@/api/session';
import { createQueryClient } from '@/lib/queryClient';
import {
  fakeSession,
  INVALID_CREDENTIALS_DETAIL,
  SESSION_PATH,
  server,
  TEST_PASSWORD,
  TEST_USERNAME,
  TOO_MANY_ATTEMPTS_DETAIL,
} from '@/test/server';

/** Renders `useSession` against the shipped query client, never a test-only one. */
function renderSession() {
  const queryClient = createQueryClient();

  return renderHook(() => useSession(), {
    wrapper: ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client: queryClient }, children),
  });
}

/** Runs a call that is expected to reject and hands back the rejection. */
async function captureRejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error: unknown) {
    return error;
  }

  throw new Error('Expected the call to reject, but it resolved.');
}

describe('useSession', () => {
  it('reads the signed-in username from /api/auth/session', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    const { result } = renderSession();

    await waitFor(() => {
      expect(result.current.isPending).toBe(false);
    });
    expect(result.current.data).toEqual({ username: TEST_USERNAME });
    expect(result.current.isError).toBe(false);
  });

  it('resolves to null rather than rejecting when there is no session', async () => {
    // The default handler answers with the backend's 401 problem document.
    const { result } = renderSession();

    await waitFor(() => {
      expect(result.current.isPending).toBe(false);
    });
    // This is the decision the whole guard rests on: "signed out" collapses
    // into `data`, so `error` is left meaning only "we could not find out".
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);

    // The `queryFn` has to swallow the 401 itself. `createQueryClient`'s
    // mid-session rule would also write `null` here, which makes the redirect
    // still happen with the catch deleted - so asserting only on `data` proves
    // nothing about this module. A signed-out visit must never be *recorded*
    // as a failed fetch: a failure count drives retry policy, error reporting
    // and the devtools, and a normal signed-out page load is not a failure.
    expect(result.current.failureCount).toBe(0);
    expect(result.current.errorUpdateCount).toBe(0);
  });

  it('still rejects when the session genuinely cannot be read', async () => {
    server.use(
      http.get(SESSION_PATH, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Internal Server Error',
            status: 500,
            detail: 'The server encountered an unexpected condition.',
          },
          { status: 500, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const { result } = renderSession();

    await waitFor(() => {
      expect(result.current.isError).toBe(true);
    });
    expect(result.current.data).toBeUndefined();
  });
});

describe('login', () => {
  it('signs in with the submitted credentials', async () => {
    const fake = fakeSession();
    server.use(...fake.handlers);

    await expect(
      login({ username: TEST_USERNAME, password: TEST_PASSWORD }),
    ).resolves.toBeUndefined();

    expect(fake.logins).toHaveLength(1);
    expect(fake.logins[0]?.body).toEqual({ username: TEST_USERNAME, password: TEST_PASSWORD });
    expect(fake.currentUser()).toBe(TEST_USERNAME);
  });

  it('rejects with the server problem when the credentials are refused', async () => {
    server.use(...fakeSession().handlers);

    const rejection = await captureRejection(
      login({ username: TEST_USERNAME, password: 'wrong-password' }),
    );

    expect(rejection).toBeInstanceOf(ApiError);
    const error = rejection as ApiError;
    expect(error.status).toBe(401);
    expect(error.problem.detail).toBe(INVALID_CREDENTIALS_DETAIL);
  });

  it('surfaces the throttle as a 429 the caller can recognise', async () => {
    server.use(
      http.post('/api/auth/login', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Too Many Requests',
            status: 429,
            detail: TOO_MANY_ATTEMPTS_DETAIL,
          },
          { status: 429, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(
      login({ username: TEST_USERNAME, password: TEST_PASSWORD }),
    );

    expect((rejection as ApiError).status).toBe(429);
  });
});

describe('logout', () => {
  it('revokes the session with a write that declares a JSON body', async () => {
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);

    await expect(logout()).resolves.toBeUndefined();

    expect(fake.currentUser()).toBeNull();
    // `POST /api/auth/logout` carries no body, and the backend's write guard is
    // deliberately not relaxed for bodyless requests. Setting the header only
    // when there is a body is a `403` waiting to happen.
    expect(fake.logouts[0]?.contentType).toBe('application/json');
  });
});
