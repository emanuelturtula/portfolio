import { screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { useNavigate } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { fakePortfolio } from '@/test/fakePortfolio';
import { currentPath, renderApp, settle, visitedPaths } from '@/test/render';
import {
  fakeSession,
  INVALID_CREDENTIALS_DETAIL,
  LOGIN_PATH,
  server,
  SESSION_PATH,
  TEST_PASSWORD,
  TEST_USERNAME,
  TOO_MANY_ATTEMPTS_DETAIL,
} from '@/test/server';

/**
 * Every imperative `navigate(...)` application code performs, in order.
 *
 * This is white-box, and deliberately so. The redirect after a successful
 * sign-in must be a single declarative `<Navigate>`: an imperative call racing
 * the declarative branch is what made a real sign-in from `/health` land on
 * `/`, confirmed against the running backend. That race cannot be caught from
 * the DOM here - measured, not assumed: with the race restored, jsdom produces
 * the byte-identical trajectory `["/health","/login","/health"]` and the same
 * final location as the fixed code, because it resolves the ordering the safe
 * way while a real browser resolves it the other way.
 *
 * So the behavioural assertions below stay exactly as they are, and this adds
 * the one property that *is* observable here: during a sign-in, nothing
 * navigates imperatively at all. The mock only intercepts calls made through
 * this package's public export, which is application code - `<Navigate>` uses
 * react-router's own internal hook and never appears here.
 */
const { imperativeNavigations } = vi.hoisted(() => ({
  imperativeNavigations: [] as unknown[],
}));

vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>();

  return {
    ...actual,
    useNavigate: () => {
      const navigate = actual.useNavigate();

      return (...args: Parameters<typeof navigate>) => {
        imperativeNavigations.push(args[0]);
        return navigate(...args);
      };
    },
  };
});

beforeEach(() => {
  imperativeNavigations.length = 0;
  // Signing in lands on the dashboard, which reads the portfolio. An empty one
  // is the first-time owner, and it keeps those reads answered rather than
  // failing as unhandled requests that put a second alert on the page.
  server.use(...fakePortfolio().handlers);
});

const PROBE_LABEL = 'probe: navigate imperatively';
const PROBE_DESTINATION = '/health';

/**
 * The positive control for {@link imperativeNavigations}.
 *
 * Application code performs no imperative navigation at all any more, which
 * is the property under test - and it leaves the recorder with nothing to
 * record, so an empty buffer would look identical to a recorder that had
 * silently stopped working. This component is the one thing in the render
 * that navigates imperatively on purpose.
 */
function NavigationProbe() {
  const navigate = useNavigate();

  return (
    <button
      type="button"
      onClick={() => {
        void navigate(PROBE_DESTINATION);
      }}
    >
      {PROBE_LABEL}
    </button>
  );
}

/**
 * Waits for the sign-in to complete and for the app to stop moving.
 *
 * The distinction is load-bearing. `waitFor(() => expect(currentPath())
 * .toBe('/health'))` is satisfied the instant that path appears, so it passed
 * against an implementation that reached `/health` and was then overridden by
 * a second, hardcoded redirect to `/` one tick later - a bug that reached the
 * running app while this file was green. Settling first, then asserting once,
 * is what makes the assertion about where the user is left.
 */
async function signInAndSettle(user: UserEvent): Promise<void> {
  await signIn(user);
  await waitFor(() => {
    expect(loginFormIsShown()).toBe(false);
  });
  await settle();
}

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
    await signInAndSettle(user);

    // Signed in straight from `/login`, so there is no captured location and
    // `/` is the default rather than a fallback something else fell back to.
    expect(currentPath()).toBe('/');
    expect(visitedPaths().at(-1)).toBe('/');
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

    await signInAndSettle(user);

    // Where the user is left, after everything queued has run.
    expect(currentPath()).toBe('/health');
    // And nothing bounced them somewhere else afterwards. This is the
    // assertion the previous version of this test was missing.
    expect(visitedPaths().at(-1)).toBe('/health');
    expect(await screen.findByText(/backend health/i)).toBeInTheDocument();

    // This test is also the positive control for the two open-redirect cases
    // below: it proves `/health` really was reachable, so "landed on `/`"
    // there means the path was refused rather than never honoured at all.
  });

  it('redirects declaratively, so nothing can race the redirect', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);

    renderApp(['/health'], <NavigationProbe />);
    await screen.findByLabelText(/username/i);

    await signInAndSettle(user);

    expect(currentPath()).toBe('/health');
    // See the comment on `imperativeNavigations`. Two redirects reaching for
    // the same location is a race whose outcome differs between jsdom and a
    // browser, so the rule is one redirect, not a faster one.
    expect(imperativeNavigations).toEqual([]);

    // The positive control, on the same buffer, in the same test. `toEqual([])`
    // is satisfied just as well by a recorder that silently stopped recording -
    // a mock that no longer intercepts, an import that moved, a refactor that
    // renamed the export - and it would then pass against the very bug it
    // exists to catch. `NavigationProbe` is the one thing in this render that
    // deliberately navigates imperatively, so clicking it proves the buffer was
    // live for the whole test and that the empty assertion above meant
    // something.
    await user.click(screen.getByRole('button', { name: PROBE_LABEL }));
    await waitFor(() => {
      expect(imperativeNavigations).toEqual([PROBE_DESTINATION]);
    });
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

      await signInAndSettle(user);

      // There is exactly one redirect, and its destination is whatever
      // `resolveReturnPath` returned - so the settled location *is* that
      // decision, with none of the ambiguity the old dual-redirect
      // implementation had.
      expect(currentPath()).toBe('/');
      expect(visitedPaths().at(-1)).toBe('/');

      // Stronger than the destination alone: the router must never have gone
      // there at all after the sign-in. The only permitted appearance is the
      // attempted entry the guard bounced off in the first place.
      expect(visitedPaths().filter((path) => path.includes('evil.example'))).toEqual([attempted]);
    },
  );

  it('says so when the sign-in worked but the session could not be confirmed', async () => {
    const user = userEvent.setup();
    const fake = fakeSession();
    server.use(...fake.handlers);
    // The credentials are accepted, and the read that confirms who now holds
    // the cookie then fails. Without its own branch the user sits on a login
    // form while already signed in: `session.data` is `undefined`, not `null`,
    // so neither the redirect nor the "signed out" path fires.
    server.use(http.get(SESSION_PATH, () => HttpResponse.error()));

    renderApp(['/login']);
    await signIn(user);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/signed in, but we could not confirm it/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
    // Not the credentials message: the credentials were fine.
    expect(alert).not.toHaveTextContent(INVALID_CREDENTIALS_DETAIL);
    expect(currentPath()).toBe('/login');
    // The sign-in really did happen, whatever the confirmation says.
    expect(fake.logins).toHaveLength(1);
  });

  it('recovers when the retry confirms the session', async () => {
    const user = userEvent.setup();
    const fake = fakeSession();
    server.use(...fake.handlers);
    server.use(http.get(SESSION_PATH, () => HttpResponse.error()));

    renderApp(['/login']);
    await signIn(user);
    await screen.findByRole('alert');

    // Retry re-issues the session read rather than reloading the page, and the
    // now-confirmed session carries the user onward.
    server.use(...fake.handlers);
    await user.click(screen.getByRole('button', { name: /try again/i }));

    await waitFor(() => {
      expect(currentPath()).toBe('/');
    });
    await settle();
    expect(currentPath()).toBe('/');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

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
    // Criterion 4: the failure is shown through the shared `ErrorState`, not a
    // bespoke one-off. The heading is what tells them apart - a bare
    // `<p role="alert">` carries the sentence but no summary above it - and
    // two error states that look different is how a shared primitive quietly
    // stops being shared.
    expect(within(alert).getByRole('heading')).toBeInTheDocument();
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
