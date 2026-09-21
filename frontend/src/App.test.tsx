import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http } from 'msw';
import { describe, expect, it } from 'vitest';

import { currentPath, renderApp } from '@/test/render';
import { fakeSession, HEALTH_PATH, server, TEST_USERNAME, unauthorized } from '@/test/server';

/** True while the login form is on screen. */
function loginFormIsShown(): boolean {
  return screen.queryByLabelText(/password/i) !== null;
}

describe('App', () => {
  it('redirects an unauthenticated visit to a protected route to the login page', async () => {
    renderApp(['/health']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    expect(loginFormIsShown()).toBe(true);
  });

  it('redirects an unauthenticated visit to an unknown route to the login page', async () => {
    // The catch-all route is protected too. An unknown path must not be a hole
    // through which an unauthenticated visitor sees anything at all.
    renderApp(['/nowhere-at-all']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    expect(loginFormIsShown()).toBe(true);
  });

  it('shows the not-found page for an unknown route when signed in', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderApp(['/nowhere-at-all']);

    expect(await screen.findByText(/not found/i)).toBeInTheDocument();
    expect(loginFormIsShown()).toBe(false);
  });

  it('renders the dashboard placeholder rather than a blank page', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderApp(['/']);

    const main = await screen.findByRole('main');
    await waitFor(() => {
      expect(main.textContent.trim().length).toBeGreaterThan(0);
    });
    // A placeholder, not a failure and not a permanent loading state.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('keeps the health page reachable, behind the guard', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderApp(['/health']);

    expect(await screen.findByText(/backend health/i)).toBeInTheDocument();
    expect(currentPath()).toBe('/health');
  });

  it('returns to the login page when a protected query answers 401', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);
    // The session read succeeds and the page's own read does not: the session
    // died between the two calls, which is what a 7-day idle window plus a
    // restarted backend produces in practice.
    server.use(http.get(HEALTH_PATH, () => unauthorized()));

    renderApp(['/health']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    // Not a crash and not a blank screen: a usable form.
    expect(loginFormIsShown()).toBe(true);
    expect(screen.getByRole('button', { name: /sign in/i })).toBeInTheDocument();
  });

  it('signs out and returns to the login page', async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);

    renderApp(['/']);

    await user.click(await screen.findByRole('button', { name: /sign out/i }));

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    expect(loginFormIsShown()).toBe(true);
    expect(fake.currentUser()).toBeNull();

    // `POST /api/auth/logout` carries no body. The backend's write guard still
    // requires `Content-Type: application/json` on it, and the handler in
    // `src/test/server.ts` refuses the request with a 403 when it is missing -
    // so setting the header only when there is a body fails right here.
    expect(fake.logouts).toHaveLength(1);
    expect(fake.logouts[0]?.contentType).toBe('application/json');
  });

  it('offers no sign-out control while signed out', async () => {
    renderApp(['/login']);

    await screen.findByLabelText(/username/i);
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument();
  });
});
