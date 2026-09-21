import { screen, waitFor } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { currentPath, renderApp } from '@/test/render';
import {
  fakeSession,
  INVALID_CREDENTIALS_DETAIL,
  LOGIN_PATH,
  server,
  TEST_PASSWORD,
  TEST_USERNAME,
  TOO_MANY_ATTEMPTS_DETAIL,
} from '@/test/server';

/**
 * Every destination `LoginPage` asks the router for, in order.
 *
 * Asserting on where the router *ended up* is not enough for the open-redirect
 * case, and finding that out is the whole reason this exists. A `MemoryRouter`
 * under jsdom quietly swallows a protocol-relative destination, and
 * `LoginPage`'s own `<Navigate to="/">` - which fires as soon as the session
 * query reports a user - then lands the test on `/` regardless. The end-to-end
 * assertion therefore passes just as happily against a `resolveReturnPath`
 * that accepts `//evil.example` as against one that refuses it.
 *
 * What is actually under test is the decision the page makes, so that is what
 * is recorded. The real `useNavigate` still runs underneath, so nothing else
 * in this file changes behaviour.
 */
const { navigateCalls } = vi.hoisted(() => ({ navigateCalls: [] as unknown[] }));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();

  return {
    ...actual,
    useNavigate: () => {
      const navigate = actual.useNavigate();

      return (...args: Parameters<typeof navigate>) => {
        navigateCalls.push(args[0]);
        return navigate(...args);
      };
    },
  };
});

beforeEach(() => {
  navigateCalls.length = 0;
});

/** Fills in the form and submits it, the way a person would. */
async function signIn(
  user: UserEvent,
  credentials: { username?: string; password?: string } = {},
): Promise<void> {
  await user.type(await screen.findByLabelText(/username/i), credentials.username ?? TEST_USERNAME);
  await user.type(screen.getByLabelText(/password/i), credentials.password ?? TEST_PASSWORD);
  await user.click(screen.getByRole('button', { name: /sign in/i }));
}

/** True while the login form is on screen. */
function loginFormIsShown(): boolean {
  return screen.queryByLabelText(/password/i) !== null;
}

describe('LoginPage', () => {
  it('lands on the dashboard after a successful sign-in', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);

    renderApp(['/login']);
    await signIn(user);

    await waitFor(() => {
      expect(currentPath()).toBe('/');
    });
    expect(loginFormIsShown()).toBe(false);
  });

  it('returns to the route that triggered the redirect', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);

    // Start on a protected route while signed out. The guard is what captures
    // the attempted location, so this exercises the real handshake rather than
    // a hand-written piece of router state that could drift from it.
    renderApp(['/health']);
    await screen.findByLabelText(/username/i);
    expect(currentPath()).toBe('/login');

    await signIn(user);

    await waitFor(() => {
      expect(currentPath()).toBe('/health');
    });
    // The positive control for the open-redirect test below: a legitimate
    // captured path really is the one the page asks the router for. Without
    // this, a `resolveReturnPath` that always returned `/` would satisfy every
    // "must not go to evil.example" assertion in the file.
    expect(navigateCalls).toContain('/health');
  });

  // A protocol-relative path is the cheap open redirect: it looks local
  // because it starts with a slash, and a browser reads it as another origin
  // entirely. The backslash spelling is the one that gets missed - WHATWG URL
  // parsing normalises a backslash to a forward slash, so `/\evil.example`
  // reaches exactly the same place as `//evil.example`.
  it.each(['//evil.example', '/\\evil.example'])(
    'ignores a non-local return path: %j',
    async (attempted) => {
      const user = userEvent.setup();
      server.use(...fakeSession().handlers);

      renderApp([attempted]);
      await screen.findByLabelText(/username/i);

      await signIn(user);

      await waitFor(() => {
        expect(navigateCalls.length).toBeGreaterThan(0);
      });
      // The load-bearing assertion: the page must never *ask* to go there.
      expect(navigateCalls).not.toContain(attempted);
      expect(JSON.stringify(navigateCalls)).not.toContain('evil.example');
      expect(navigateCalls).toContain('/');

      await waitFor(() => {
        expect(currentPath()).toBe('/');
      });
      expect(currentPath()).not.toContain('evil.example');
    },
  );

  it('redirects away from the login form when a session already exists', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderApp(['/login']);

    // Otherwise the back button lands the owner on a form they cannot leave.
    await waitFor(() => {
      expect(currentPath()).toBe('/');
    });
    expect(loginFormIsShown()).toBe(false);
  });

  it("shows the server's message when the credentials are refused", async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);

    renderApp(['/login']);
    await signIn(user, { password: 'not-the-password' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(INVALID_CREDENTIALS_DETAIL);
    expect(currentPath()).toBe('/login');
    // The form stays usable: one typo must not need a page reload.
    expect(screen.getByRole('button', { name: /sign in/i })).toBeEnabled();
  });

  it('shows the retry-later message on 429', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);
    server.use(
      http.post(LOGIN_PATH, () =>
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

    renderApp(['/login']);
    await signIn(user);

    const alert = await screen.findByRole('alert');
    // "Try again later" and "wrong password" are different problems, and a
    // throttled owner who is told their password is wrong will change it.
    expect(alert).toHaveTextContent(TOO_MANY_ATTEMPTS_DETAIL);
    expect(alert).not.toHaveTextContent(INVALID_CREDENTIALS_DETAIL);
    expect(currentPath()).toBe('/login');
  });

  it('shows an unreachable-backend message when the request fails outright', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);
    server.use(http.post(LOGIN_PATH, () => HttpResponse.error()));

    renderApp(['/login']);
    await signIn(user);

    const alert = await screen.findByRole('alert');
    // A `TypeError` from `fetch` is not an `ApiError` and has no problem
    // document, so the page has to have a sentence of its own. Leaking
    // "Failed to fetch" is the failure this covers.
    expect(alert).toHaveTextContent(/reach|unavailable|unreachable|connect|running/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
    expect(alert).not.toHaveTextContent(INVALID_CREDENTIALS_DETAIL);
    expect(currentPath()).toBe('/login');
  });

  it('falls back to the problem title when the server sends no detail', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);
    server.use(
      http.post(LOGIN_PATH, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Service Unavailable', status: 503 },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    renderApp(['/login']);
    await signIn(user);

    // The backend serialises with `exclude_none=True`, so a problem document
    // genuinely arrives without a `detail` member.
    expect(await screen.findByRole('alert')).toHaveTextContent('Service Unavailable');
  });

  it('disables the submit button while the request is in flight', async () => {
    const user = userEvent.setup();
    const fake = fakeSession();
    server.use(...fake.handlers);

    // This override replaces the fake's own login route, so the attempts are
    // counted here rather than in `fake.logins`.
    let attempts = 0;
    server.use(
      http.post(LOGIN_PATH, async () => {
        attempts += 1;
        await delay(100);
        fake.signIn();
        return new HttpResponse(null, { status: 204 });
      }),
    );

    renderApp(['/login']);
    await signIn(user);

    // A double submit is two password verifications against a throttle that
    // counts them, on a Raspberry Pi where Argon2id takes real time.
    expect(screen.getByRole('button', { name: /sign in/i })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => {
      expect(currentPath()).toBe('/');
    });
    expect(attempts).toBe(1);
  });

  it('sends the credentials exactly as typed', async () => {
    const user = userEvent.setup();
    const fake = fakeSession();
    server.use(...fake.handlers);

    renderApp(['/login']);
    await signIn(user);

    await waitFor(() => {
      expect(fake.logins).toHaveLength(1);
    });
    expect(fake.logins[0]?.body).toEqual({
      username: TEST_USERNAME,
      password: TEST_PASSWORD,
    });
    expect(fake.logins[0]?.contentType).toBe('application/json');
  });
});
