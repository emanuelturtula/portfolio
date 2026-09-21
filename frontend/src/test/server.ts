import { http, HttpResponse, type HttpHandler } from 'msw';
import { setupServer } from 'msw/node';

/** Body the default `GET /api/health` handler answers with. */
export const healthFixture = {
  status: 'ok',
  version: '0.1.0',
  environment: 'test',
};

export const HEALTH_PATH = '/api/health';
export const SESSION_PATH = '/api/auth/session';
export const LOGIN_PATH = '/api/auth/login';
export const LOGOUT_PATH = '/api/auth/logout';

/**
 * The exact sentences the backend answers with, copied from
 * `backend/src/portfolio/services/auth.py`. Written out literally rather than
 * generated, so that a wording change on either side shows up as a diff here
 * instead of quietly agreeing with itself.
 */
export const SESSION_REQUIRED_DETAIL = 'Authentication is required.';
export const INVALID_CREDENTIALS_DETAIL = 'The username or password is incorrect.';
export const TOO_MANY_ATTEMPTS_DETAIL = 'Too many failed attempts. Try again later.';

/** Builds an RFC 9457 problem document with the media type the backend uses. */
export function problem(status: number, title: string, detail: string): Response {
  return HttpResponse.json(
    { type: 'about:blank', title, status, detail },
    { status, headers: { 'content-type': 'application/problem+json' } },
  );
}

/** The document every endpoint answers with when there is no usable session. */
export function unauthorized(): Response {
  return problem(401, 'Unauthorized', SESSION_REQUIRED_DETAIL);
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);
const JSON_MEDIA_TYPE = 'application/json';

/**
 * Mirrors the backend's write guard: a request whose method is not safe and
 * which does not declare `Content-Type: application/json` is refused with a
 * `403` before it reaches a route, body or no body.
 *
 * This exists so that dropping the unconditional `Content-Type` from the client
 * breaks a test rather than only breaking production. `POST /api/auth/logout`
 * has no body, which is exactly the case a `body !== undefined` check misses.
 *
 * Returns the refusal, or `undefined` when the request is acceptable.
 */
export function refuseNonJsonWrite(request: Request): Response | undefined {
  if (SAFE_METHODS.has(request.method.toUpperCase())) {
    return undefined;
  }

  const declared = (request.headers.get('content-type') ?? '').split(';')[0]?.trim().toLowerCase();

  if (declared === JSON_MEDIA_TYPE) {
    return undefined;
  }

  return problem(
    403,
    'Forbidden',
    'A state-changing request must declare a JSON body with Content-Type: application/json.',
  );
}

/** What a write actually carried, recorded so a test can assert on it. */
export interface RecordedWrite {
  readonly contentType: string | null;
  readonly body: unknown;
}

/**
 * The credentials {@link fakeSession} accepts unless a test says otherwise.
 * Synthetic, and deliberately nothing that resembles a real one.
 */
export const TEST_USERNAME = 'owner';
export const TEST_PASSWORD = 'correct-horse-battery-staple';

export interface FakeSessionOptions {
  /** Who is signed in when the test starts. `null` means signed out. */
  readonly initialUser?: string | null;
  readonly username?: string;
  readonly password?: string;
}

export interface FakeSession {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  readonly logins: RecordedWrite[];
  readonly logouts: RecordedWrite[];
  currentUser(): string | null;
  signIn(username?: string): void;
  signOut(): void;
}

/**
 * Models the backend's session state in the handlers rather than in a cookie.
 *
 * `jsdom` has no usable jar for the `__Host-` prefixed, `Secure` cookie the
 * backend sets, so a cookie round-trip cannot be reproduced here at all. The
 * blind spot is deliberate and is covered by #3's backend tests; see the Risks
 * section of `docs/specs/004-login-page-and-app-shell.md`.
 */
export function fakeSession(options: FakeSessionOptions = {}): FakeSession {
  const expectedUsername = options.username ?? TEST_USERNAME;
  const expectedPassword = options.password ?? TEST_PASSWORD;

  let currentUser: string | null = options.initialUser ?? null;
  const logins: RecordedWrite[] = [];
  const logouts: RecordedWrite[] = [];

  const handlers: HttpHandler[] = [
    http.get(SESSION_PATH, () =>
      currentUser === null ? unauthorized() : HttpResponse.json({ username: currentUser }),
    ),

    http.post(LOGIN_PATH, async ({ request }) => {
      const body = await readJsonBody(request);
      logins.push({ contentType: request.headers.get('content-type'), body });

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      const credentials = body as { username?: unknown; password?: unknown } | undefined;

      if (credentials?.username !== expectedUsername || credentials.password !== expectedPassword) {
        return problem(401, 'Unauthorized', INVALID_CREDENTIALS_DETAIL);
      }

      currentUser = expectedUsername;
      return new HttpResponse(null, { status: 204 });
    }),

    http.post(LOGOUT_PATH, async ({ request }) => {
      const body = await readJsonBody(request);
      logouts.push({ contentType: request.headers.get('content-type'), body });

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      if (currentUser === null) {
        return unauthorized();
      }

      currentUser = null;
      return new HttpResponse(null, { status: 204 });
    }),
  ];

  return {
    handlers,
    logins,
    logouts,
    currentUser: () => currentUser,
    signIn: (username = expectedUsername) => {
      currentUser = username;
    },
    signOut: () => {
      currentUser = null;
    },
  };
}

/** Reads a request body as JSON, tolerating the bodyless writes this API has. */
async function readJsonBody(request: Request): Promise<unknown> {
  try {
    return (await request.clone().json()) as unknown;
  } catch {
    return undefined;
  }
}

/**
 * The baseline: health answers, and the session endpoint says "signed out".
 *
 * Signed out is the safe default. A test that needs a session registers
 * `fakeSession({ initialUser: ... }).handlers`, so no test can drift into
 * asserting authenticated behaviour without having said so.
 */
export const handlers: HttpHandler[] = [
  http.get(HEALTH_PATH, () => HttpResponse.json(healthFixture)),
  http.get(SESSION_PATH, () => unauthorized()),
];

/**
 * Request interception for the whole test run. Individual tests override a
 * route with `server.use(...)`; `setup.ts` resets those overrides after each
 * test so they never leak.
 */
export const server = setupServer(...handlers);
