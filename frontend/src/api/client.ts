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
 */

const JSON_MEDIA_TYPE = 'application/json';

/** Fallback title used when the server gives us nothing better to show. */
const FALLBACK_TITLE = 'Request failed';

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

  constructor(problem: ProblemDetails) {
    super(problem.detail ?? problem.title);
    this.name = 'ApiError';
    this.problem = problem;
    this.status = problem.status;
  }
}

export interface ApiRequestOptions {
  readonly method?: string;
  /** Serialised as JSON; its presence is what sets the `Content-Type` header. */
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
  const headers = new Headers({ Accept: JSON_MEDIA_TYPE });
  const init: RequestInit = {
    method: options.method ?? 'GET',
    // The backend authenticates with a session cookie, so every call must carry
    // credentials - including the proxied ones in development.
    credentials: 'include',
    headers,
  };

  if (options.body !== undefined) {
    headers.set('Content-Type', JSON_MEDIA_TYPE);
    init.body = JSON.stringify(options.body);
  }

  if (options.signal !== undefined) {
    init.signal = options.signal;
  }

  const response = await fetch(resolveUrl(path), init);

  if (!response.ok) {
    throw new ApiError(await readProblem(response));
  }

  const payload = await readJson(response);

  if (payload === undefined) {
    throw new ApiError({
      type: 'about:blank',
      title: 'Malformed response',
      status: response.status,
      detail: 'The server answered with a successful status but the body was not valid JSON.',
    });
  }

  return payload as T;
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

/** Maps a failed response onto problem details, whatever the body looks like. */
async function readProblem(response: Response): Promise<ProblemDetails> {
  const fallbackTitle = response.statusText === '' ? FALLBACK_TITLE : response.statusText;
  const body = await readJson(response);

  if (!isRecord(body)) {
    return { type: 'about:blank', title: fallbackTitle, status: response.status };
  }

  const status = body.status;

  return {
    type: readString(body, 'type') ?? 'about:blank',
    title: readString(body, 'title') ?? fallbackTitle,
    status: typeof status === 'number' ? status : response.status,
    detail: readString(body, 'detail'),
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readString(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === 'string' ? value : undefined;
}
