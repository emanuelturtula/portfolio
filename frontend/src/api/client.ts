/**
 * Minimal typed wrapper around `fetch` for talking to the backend API.
 *
 * The backend reports every failure as an RFC 9457 problem document
 * (`application/problem+json`) with the members `type`, `title`, `status` and
 * `detail`. This wrapper turns such a response into an {@link ApiError} so that
 * callers never have to inspect `response.ok` themselves, and so that the UI
 * always has a human-readable sentence to show. Responses that are not valid
 * JSON at all - an HTML error page from a proxy, an empty body - are mapped
 * onto the same shape instead of surfacing a raw `SyntaxError`.
 *
 * All requests are same-origin: the Vite dev server proxies `/api` to the
 * backend, and in production both are served from the same origin.
 *
 * Two entry points share one internal {@link request} so they cannot drift:
 * {@link apiFetch} for endpoints that answer with a JSON body, and
 * {@link apiSend} for the `204 No Content` responses the auth endpoints use,
 * which must not be parsed as JSON at all.
 */

const JSON_MEDIA_TYPE = 'application/json';

/** Fallback title used when the server gives us nothing better to show. */
const FALLBACK_TITLE = 'Request failed';

/**
 * Methods for which a body - and therefore a `Content-Type` - would be
 * unusual. Every other method gets `Content-Type: application/json` even when
 * it carries no body, because the backend's write guard requires that header
 * on every non-safe method and does not relax it for a bodyless request (see
 * `POST /api/auth/logout`). Setting it at the call site instead would leave
 * every future write one forgotten header away from a `403`.
 */
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * The subset of RFC 9457 problem details this application relies on.
 *
 * `type` defaults to `about:blank`, as the RFC prescribes, when the server
 * omits it.
 */
export interface ProblemDetails {
  readonly type: string;
  readonly title: string;
  readonly status: number;
  readonly detail?: string | undefined;
}

/** Error thrown for any non-OK API response, carrying the problem document. */
export class ApiError extends Error {
  readonly problem: ProblemDetails;

  readonly status: number;

  /**
   * Whether `problem` is a real RFC 9457 document the server sent, or one
   * synthesised here because the body was not one - an HTML proxy
   * interstitial for a `502`, an empty body, anything not
   * `application/problem+json`. A synthesised `problem.title` is borrowed
   * from `response.statusText` (`"Bad Gateway"`, `"Request failed"`), not
   * written for a person to read, so {@link describeApiError} must not treat
   * it the way it treats a title the backend actually wrote.
   */
  readonly hasProblemDocument: boolean;

  constructor(problem: ProblemDetails, hasProblemDocument: boolean) {
    super(problem.detail ?? problem.title);
    this.name = 'ApiError';
    this.problem = problem;
    this.status = problem.status;
    this.hasProblemDocument = hasProblemDocument;
  }
}

/**
 * Turns any error a query or mutation can fail with into one sentence a user
 * can read: the server's own `problem.detail`, or `problem.title` when that
 * title was actually written by the server, for an {@link ApiError}; the
 * given `fallback` otherwise - a network failure, an unreachable backend, or
 * an `ApiError` whose problem document was synthesised from a non-JSON
 * response rather than sent by the backend (see {@link ApiError.hasProblemDocument}).
 *
 * That last case is why the fallback cannot simply lose to `problem.title`
 * unconditionally: with the backend unreachable, a proxy's own `502` page is
 * not JSON, `title` becomes its HTTP reason phrase, and every caller's
 * carefully written fallback - "your session may still be active" and
 * similar - would otherwise be silently unreachable for exactly the failures
 * it exists to describe.
 *
 * The backend already writes the sentence a person should see into a real
 * problem document. Every call site that showed its own wording for an
 * `ApiError` was a second copy of that sentence waiting to drift from the
 * first, so this is the one place that reads it.
 */
export function describeApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) {
    return fallback;
  }

  return error.problem.detail ?? (error.hasProblemDocument ? error.problem.title : fallback);
}

export interface ApiRequestOptions {
  readonly method?: string;
  /**
   * Serialised as JSON when present. `Content-Type` is set independently of
   * this - see {@link SAFE_METHODS} - so a bodyless write still declares the
   * media type the backend's write guard requires.
   */
  readonly body?: unknown;
  readonly signal?: AbortSignal;
}

/**
 * Performs a JSON request against the API and returns the parsed body.
 *
 * @typeParam T - Shape of the successful response body. Nothing is validated at
 * runtime yet; once `npm run gen:api` is wired up, `T` comes from the generated
 * OpenAPI types.
 * @throws {ApiError} When the response status is not in the 2xx range, or when
 * a successful response does not contain valid JSON.
 */
export async function apiFetch<T>(path: string, options: ApiRequestOptions = {}): Promise<T> {
  const response = await request(path, options);
  const payload = await readJson(response);

  if (payload === undefined) {
    throw new ApiError(
      {
        type: 'about:blank',
        title: 'Malformed response',
        status: response.status,
        detail: 'The server answered with a successful status but the body was not valid JSON.',
      },
      false,
    );
  }

  return payload as T;
}

/**
 * Performs a request against an endpoint that answers `204 No Content` on
 * success - every auth endpoint except the session read. The body is never
 * read: a `204` has none, and reading it as JSON is exactly what makes
 * {@link apiFetch} unusable against these endpoints.
 *
 * @throws {ApiError} When the response status is not in the 2xx range.
 */
export async function apiSend(path: string, options: ApiRequestOptions = {}): Promise<void> {
  await request(path, options);
}

/**
 * Shared request plumbing for {@link apiFetch} and {@link apiSend}: builds the
 * headers, serialises the body, sends the request and turns a non-OK response
 * into an {@link ApiError}. Neither caller reads the body here - that is each
 * one's own concern - so this returns the raw {@link Response}.
 */
async function request(path: string, options: ApiRequestOptions): Promise<Response> {
  const method = options.method ?? 'GET';
  const headers = new Headers({ Accept: JSON_MEDIA_TYPE });

  if (!SAFE_METHODS.has(method.toUpperCase())) {
    headers.set('Content-Type', JSON_MEDIA_TYPE);
  }

  const init: RequestInit = {
    method,
    // The backend authenticates with a session cookie, so every call must carry
    // credentials - including the proxied ones in development.
    credentials: 'include',
    headers,
  };

  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }

  if (options.signal !== undefined) {
    init.signal = options.signal;
  }

  const response = await fetch(resolveUrl(path), init);

  if (!response.ok) {
    const { problem, hasProblemDocument } = await readProblem(response);
    throw new ApiError(problem, hasProblemDocument);
  }

  return response;
}

/** Resolves an API path against the current origin, which `fetch` requires. */
function resolveUrl(path: string): string {
  return new URL(path, window.location.origin).toString();
}

/**
 * Reads a response body as JSON, returning `undefined` when it cannot be
 * parsed. JSON itself can never produce `undefined`, so it is an unambiguous
 * "not JSON" marker.
 */
async function readJson(response: Response): Promise<unknown> {
  try {
    return (await response.json()) as unknown;
  } catch {
    return undefined;
  }
}

/** {@link readProblem}'s result: the details, and whether the server actually sent them. */
interface ReadProblemResult {
  readonly problem: ProblemDetails;
  /** `false` when `problem` was synthesised because the body was not a real problem document. */
  readonly hasProblemDocument: boolean;
}

/** Maps a failed response onto problem details, whatever the body looks like. */
async function readProblem(response: Response): Promise<ReadProblemResult> {
  const fallbackTitle = response.statusText === '' ? FALLBACK_TITLE : response.statusText;
  const body = await readJson(response);

  if (!isRecord(body)) {
    return {
      problem: { type: 'about:blank', title: fallbackTitle, status: response.status },
      hasProblemDocument: false,
    };
  }

  const status = body.status;

  return {
    problem: {
      type: readString(body, 'type') ?? 'about:blank',
      title: readString(body, 'title') ?? fallbackTitle,
      status: typeof status === 'number' ? status : response.status,
      detail: readString(body, 'detail'),
    },
    hasProblemDocument: true,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readString(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === 'string' ? value : undefined;
}
