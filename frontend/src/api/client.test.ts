import { http, HttpResponse } from 'msw';
import { describe, expect, it, vi } from 'vitest';

import { ApiError, apiFetch, apiSend, describeApiError } from '@/api/client';
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

/**
 * The 422 `errors` array `api/errors.py` renders: `{loc, msg, type}` with
 * `loc = ["body", "<field>"]`. The page maps it onto form fields, so what
 * survives the parse decides which input an error lands under.
 */
describe('problem.errors', () => {
  /** Fails a POST with a 422 whose body carries `errors` and returns the ApiError. */
  async function refusedWith(errors: unknown): Promise<ApiError> {
    server.use(
      http.post('/api/wallets', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Entity',
            status: 422,
            detail: 'The request parameters failed validation.',
            instance: '/api/wallets',
            errors,
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(
      apiFetch('/api/wallets', { method: 'POST', body: { chain_key: 'bitcoin', address: 'x' } }),
    );
    expect(rejection).toBeInstanceOf(ApiError);
    return rejection as ApiError;
  }

  it('keeps a well-formed errors array', async () => {
    const error = await refusedWith([
      { loc: ['body', 'address'], msg: 'The checksum does not match.', type: 'bad_checksum' },
      {
        loc: ['body', 'label'],
        msg: 'String should have at most 100 characters',
        type: 'string_too_long',
      },
    ]);

    expect(error.status).toBe(422);
    expect(error.problem.errors).toEqual([
      { loc: ['body', 'address'], msg: 'The checksum does not match.' },
      { loc: ['body', 'label'], msg: 'String should have at most 100 characters' },
    ]);
    // The standard members are still there alongside it.
    expect(error.problem.detail).toBe('The request parameters failed validation.');
  });

  it('keeps the order the server sent', async () => {
    const error = await refusedWith([
      { loc: ['body', 'label'], msg: 'second field first', type: 't' },
      { loc: ['body', 'address'], msg: 'first field second', type: 't' },
    ]);

    expect(error.problem.errors?.map((entry) => entry.msg)).toEqual([
      'second field first',
      'first field second',
    ]);
  });

  it('drops malformed errors entries', async () => {
    const error = await refusedWith([
      { loc: ['body', 'address'], msg: 'kept', type: 'ok' },
      // A numeric location element: an index into a list field.
      { loc: ['body', 0], msg: 'numeric loc', type: 'x' },
      // `loc` that is not an array.
      { loc: 'body.address', msg: 'string loc', type: 'x' },
      // `msg` that is not a string.
      { loc: ['body', 'address'], msg: 42, type: 'x' },
      // Members missing entirely.
      { loc: ['body', 'address'] },
      { msg: 'no loc' },
      // Not an object at all.
      'a bare string',
      null,
      ['body', 'address'],
      42,
      { loc: ['body', 'label'], msg: 'also kept', type: 'ok' },
    ]);

    // Dropped one at a time, not all or nothing: one bad entry must not take
    // a good field error down with it.
    expect(error.problem.errors).toEqual([
      { loc: ['body', 'address'], msg: 'kept' },
      { loc: ['body', 'label'], msg: 'also kept' },
    ]);
  });

  it('keeps an empty location as an empty location', async () => {
    // `[]` is an array of strings, vacuously. It maps onto no field, which is
    // the page's concern; the parse must not invent one.
    const error = await refusedWith([{ loc: [], msg: 'somewhere', type: 'x' }]);

    expect(error.problem.errors).toEqual([{ loc: [], msg: 'somewhere' }]);
  });

  it('gives an empty array when every entry is malformed', async () => {
    const error = await refusedWith([{ loc: 7, msg: null }]);

    expect(error.problem.errors).toEqual([]);
  });

  it.each([
    ['an object', { address: 'The checksum does not match.' }],
    ['a string', 'The checksum does not match.'],
    ['null', null],
  ])('ignores an errors member that is %s', async (_label, errors) => {
    const error = await refusedWith(errors);

    expect(error.problem.errors).toBeUndefined();
    expect(error.problem.detail).toBe('The request parameters failed validation.');
  });

  it('has no errors when the problem document carries none', async () => {
    server.use(
      http.post('/api/wallets', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Conflict',
            status: 409,
            detail: 'This address is already registered for this chain.',
          },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    const rejection = await captureRejection(
      apiFetch('/api/wallets', { method: 'POST', body: {} }),
    );

    expect((rejection as ApiError).problem.errors).toBeUndefined();
  });

  it('does not copy the extra members of an entry', async () => {
    // Pydantic can echo the rejected `input` back in an entry. The backend
    // does not send it for an address, and if it ever did, the parse is what
    // keeps it off the page.
    const error = await refusedWith([
      {
        loc: ['body', 'address'],
        msg: 'The checksum does not match.',
        type: 'bad_checksum',
        input: 'tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx',
      },
    ]);

    expect(error.problem.errors).toEqual([
      { loc: ['body', 'address'], msg: 'The checksum does not match.' },
    ]);
    expect(JSON.stringify(error.problem)).not.toContain('tb1qw508');
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

/**
 * Which sentence the user is shown for a failed request.
 *
 * Found by stopping the real backend and pressing "Sign out": the alert read
 * "Bad Gateway", not the sentence the caller passed. Vite's proxy answers with
 * an HTML `502`, `readProblem` cannot parse it and synthesises a document whose
 * `title` is the HTTP reason phrase, and that synthesised machine phrase then
 * beat the caller's fallback. Every fallback in the application was unreachable
 * for precisely the failure it was written for.
 *
 * The fix has two halves and both need pinning, because the second is the one a
 * careless edit removes: a synthesised document must lose to the caller's
 * sentence, and a real one must still win. This project prefers the backend's
 * own wording over an invented string wherever the backend actually sent one.
 */
describe('describeApiError', () => {
  const FALLBACK = 'Could not reach the server. Your session may still be active.';

  /** Produces a genuine ApiError by failing a real request. */
  async function errorFrom(resolver: Parameters<typeof http.get>[1]): Promise<unknown> {
    server.use(http.get('/api/health', resolver));
    return captureRejection(apiFetch('/api/health'));
  }

  it('prefers the caller sentence when the problem document was synthesised', async () => {
    const error = await errorFrom(
      () =>
        new HttpResponse('<html><body>502 Bad Gateway</body></html>', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: { 'content-type': 'text/html' },
        }),
    );

    expect(describeApiError(error, FALLBACK)).toBe(FALLBACK);
    // The reason phrase is a machine word. It must not reach a person.
    expect(describeApiError(error, FALLBACK)).not.toMatch(/bad gateway/i);
  });

  it('shows the server title when the problem document is real but has no detail', async () => {
    const error = await errorFrom(() =>
      HttpResponse.json(
        // No `detail`: the backend serialises with `exclude_none=True`, so this
        // is what a real problem document without a specific message looks like.
        { type: 'about:blank', title: 'Service Unavailable', status: 503 },
        { status: 503, headers: { 'content-type': 'application/problem+json' } },
      ),
    );

    // The assertion that stops the fix overshooting into "always use the
    // fallback". The backend said something; discarding it would replace a
    // specific, true sentence with a generic guess.
    expect(describeApiError(error, FALLBACK)).toBe('Service Unavailable');
  });

  it('shows the server detail when the problem document carries one', async () => {
    const error = await errorFrom(() =>
      HttpResponse.json(
        {
          type: 'about:blank',
          title: 'Service Unavailable',
          status: 503,
          detail: 'The database is not reachable.',
        },
        { status: 503, headers: { 'content-type': 'application/problem+json' } },
      ),
    );

    expect(describeApiError(error, FALLBACK)).toBe('The database is not reachable.');
  });

  it('prefers the caller sentence when the response has no reason phrase either', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('nope', { status: 500, statusText: '' }));

    try {
      const error = await captureRejection(apiFetch('/api/health'));

      // "Request failed" is this module's own placeholder, not the server's
      // wording, so it loses to the caller's sentence for the same reason.
      expect(describeApiError(error, FALLBACK)).toBe(FALLBACK);
      expect(describeApiError(error, FALLBACK)).not.toMatch(/request failed/i);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('uses the caller sentence for a failure that is not an ApiError at all', () => {
    expect(describeApiError(new TypeError('Failed to fetch'), FALLBACK)).toBe(FALLBACK);
    expect(describeApiError(undefined, FALLBACK)).toBe(FALLBACK);
  });
});
