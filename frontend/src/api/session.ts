import { useQuery, type UseQueryResult } from '@tanstack/react-query';

import { apiFetch, apiSend, ApiError } from '@/api/client';
import type { components } from '@/api/generated/schema';

const SESSION_PATH = '/api/auth/session';
const LOGIN_PATH = '/api/auth/login';
const LOGOUT_PATH = '/api/auth/logout';

/** Who the caller is. The only thing a session read discloses (see #3). */
export type SessionUser = components['schemas']['SessionResponse'];

/** Credentials submitted by the login form. */
export type LoginCredentials = components['schemas']['LoginRequest'];

/**
 * Query key for the session read. Shared by {@link useSession}, by
 * `createQueryClient`'s mid-session 401 rule, and by anything that needs to
 * invalidate or seed the cached session directly (a successful login, a
 * logout).
 */
export const sessionQueryKey = ['session'] as const;

/**
 * Reads `GET /api/auth/session`, but resolves `null` instead of rejecting when
 * the backend answers `401`. That is the central decision this module makes:
 * a signed-out visitor is not a failed request, it is a `null` session, and
 * every other failure - a network error, a `500` - still rejects. Collapsing
 * "signed out" and "signed in" into `data` is what keeps every caller of
 * {@link useSession} from having to remember the same `status === 401` branch;
 * `error` then means only "we could not find out".
 */
async function fetchSession(signal: AbortSignal): Promise<SessionUser | null> {
  try {
    return await apiFetch<SessionUser>(SESSION_PATH, { signal });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }

    throw error;
  }
}

/** Bootstraps and tracks the current session. See {@link fetchSession}. */
export function useSession(): UseQueryResult<SessionUser | null> {
  return useQuery({
    queryKey: sessionQueryKey,
    queryFn: ({ signal }) => fetchSession(signal),
  });
}

/**
 * Exchanges credentials for a session cookie. Resolves on success (`204`) and
 * throws {@link ApiError} on `401` (invalid), `429` (throttled) or a network
 * failure. Does not itself update the session cache - the caller re-reads or
 * invalidates {@link sessionQueryKey} once the cookie is set.
 */
export async function login(credentials: LoginCredentials): Promise<void> {
  await apiSend(LOGIN_PATH, { method: 'POST', body: credentials });
}

/**
 * Revokes the current session. `POST` with no body, which is exactly the
 * request `apiSend` had to be fixed to send correctly: no body but still
 * `Content-Type: application/json`, or the backend's write guard refuses it.
 */
export async function logout(): Promise<void> {
  await apiSend(LOGOUT_PATH, { method: 'POST' });
}
