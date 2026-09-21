import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch, apiSend } from '@/api/client';
import { LOGIN_PATH, LOGOUT_PATH, server } from '@/test/server';

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

  it('fills in the members a partial problem document leaves out', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.json(
          // No `type`, no `title`, and a `status` of the wrong JSON type.
          { status: 'nonsense', detail: 42 },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(apiFetch('/api/health'));

    const error = rejection as ApiError;
    expect(error.problem.type).toBe('about:blank');
    expect(error.problem.title.length).toBeGreaterThan(0);
    // The transport status wins over a member that is not a number, so the
    // `401` handling downstream cannot be fooled by a malformed body.
    expect(error.status).toBe(503);
    expect(error.problem.detail).toBeUndefined();
  });

  it('has a title to show even when the response carries no reason phrase', async () => {
    // `fetch` is stubbed rather than intercepted here: MSW fills in the
    // standard reason phrase for a status, so an empty `statusText` cannot be
    // produced through it. HTTP/2 has no reason phrase at all, which makes an
    // empty one the normal case behind a modern proxy rather than an exotic
    // one, and a problem document with an empty title shows the user nothing.
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('nope', { status: 500, statusText: '' }));

    try {
      const rejection = await captureRejection(apiFetch('/api/health'));

      expect((rejection as ApiError).problem.title).toBe('Request failed');
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('rejects a successful response whose body is not JSON', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.text('<html>proxy interstitial</html>', {
          status: 200,
          headers: { 'content-type': 'text/html' },
        }),
      ),
    );

    const rejection = await captureRejection(apiFetch('/api/health'));

    // A reverse proxy answering 200 with an HTML page is the realistic case,
    // and a raw SyntaxError is not something a page can render.
    expect(rejection).toBeInstanceOf(ApiError);
    expect((rejection as ApiError).problem.title).toBe('Malformed response');
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

describe('apiSend', () => {
  it('resolves without a body on 204', async () => {
    server.use(http.post(LOGOUT_PATH, () => new HttpResponse(null, { status: 204 })));

    // `apiFetch` demands a JSON body and throws `Malformed response` when there
    // is none, which is what three of the four auth endpoints answer with.
    await expect(apiSend(LOGOUT_PATH, { method: 'POST' })).resolves.toBeUndefined();
  });

  it('sends application/json on a write with no body', async () => {
    let received: Request | undefined;
    server.use(
      http.post(LOGOUT_PATH, ({ request }) => {
        received = request;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await apiSend(LOGOUT_PATH, { method: 'POST' });

    // The backend's write guard refuses any non-safe method that does not
    // declare a JSON body, and is deliberately not relaxed for bodyless
    // requests. A `body !== undefined` check misses exactly this call.
    expect(received?.headers.get('content-type')).toBe('application/json');
  });

  it('still labels a write that does carry a body', async () => {
    let received: Request | undefined;
    server.use(
      http.post(LOGIN_PATH, ({ request }) => {
        received = request;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    await apiSend(LOGIN_PATH, { method: 'POST', body: { username: 'u', password: 'p' } });

    expect(received?.headers.get('content-type')).toBe('application/json');
    expect(await received?.clone().json()).toEqual({ username: 'u', password: 'p' });
  });

  it('leaves a safe method without a Content-Type header', async () => {
    let received: Request | undefined;
    server.use(
      http.get('/api/health', ({ request }) => {
        received = request;
        return HttpResponse.json({ status: 'ok' });
      }),
    );

    await apiFetch('/api/health');

    // Widening the header to every method would be the lazy fix; a GET that
    // claims to carry JSON is a lie the backend is entitled to reject.
    expect(received?.headers.get('content-type')).toBeNull();
  });

  it('raises a typed ApiError when a write is refused', async () => {
    server.use(
      http.post(LOGOUT_PATH, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unauthorized',
            status: 401,
            detail: 'Authentication is required.',
          },
          { status: 401, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(apiSend(LOGOUT_PATH, { method: 'POST' }));

    expect(rejection).toBeInstanceOf(ApiError);
    expect((rejection as ApiError).status).toBe(401);
  });
});
