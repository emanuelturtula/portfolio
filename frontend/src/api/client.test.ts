import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch } from '@/api/client';
import { server } from '@/test/server';

/** Runs a request that is expected to reject and hands back the rejection. */
async function captureRejection(promise: Promise<unknown>): Promise<unknown> {
  try {
    await promise;
  } catch (error: unknown) {
    return error;
  }

  throw new Error('Expected the request to reject, but it resolved.');
}

describe('apiFetch', () => {
  it('maps a problem+json error body onto a typed ApiError', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.json(
          {
            type: 'https://portfolio.example/problems/upstream-unavailable',
            title: 'Service Unavailable',
            status: 503,
            detail: 'The price feed did not answer in time.',
          },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(apiFetch('/api/health'));

    expect(rejection).toBeInstanceOf(ApiError);
    const error = rejection as ApiError;
    expect(error.status).toBe(503);
    expect(error.problem).toEqual({
      type: 'https://portfolio.example/problems/upstream-unavailable',
      title: 'Service Unavailable',
      status: 503,
      detail: 'The price feed did not answer in time.',
    });
    // The message is what a user ends up reading, so it must be the detail.
    expect(error.message).toBe('The price feed did not answer in time.');
  });

  it('still produces a usable error when the failing response is not JSON', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.text('<html><body>502 Bad Gateway</body></html>', {
          status: 502,
          headers: { 'content-type': 'text/html' },
        }),
      ),
    );

    const rejection = await captureRejection(apiFetch('/api/health'));

    expect(rejection).toBeInstanceOf(ApiError);
    const error = rejection as ApiError;
    expect(error.status).toBe(502);
    expect(error.problem.type).toBe('about:blank');
    expect(error.message.length).toBeGreaterThan(0);
    // No leaked "Unexpected token < in JSON" style message.
    expect(error.message).not.toMatch(/json/i);
  });

  it('sends credentials and the JSON headers the backend expects', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    try {
      await apiFetch('/api/health');

      const init = fetchSpy.mock.calls[0]?.[1];
      expect(init?.credentials).toBe('include');
      expect(new Headers(init?.headers).get('Accept')).toBe('application/json');
      // A GET has no body, so it must not claim to carry JSON.
      expect(new Headers(init?.headers).get('Content-Type')).toBeNull();
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('serialises a request body and labels it as JSON', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    server.use(http.post('/api/health', () => HttpResponse.json({ status: 'ok' })));

    try {
      await apiFetch('/api/health', { method: 'POST', body: { note: 'skeleton' } });

      const init = fetchSpy.mock.calls[0]?.[1];
      expect(init?.method).toBe('POST');
      expect(init?.body).toBe('{"note":"skeleton"}');
      expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
    } finally {
      fetchSpy.mockRestore();
    }
  });
});
