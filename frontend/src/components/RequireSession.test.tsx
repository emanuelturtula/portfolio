import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { delay, http, HttpResponse } from 'msw';
import { Route, Routes } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { RequireSession } from '@/components/RequireSession';
import { renderWithProviders } from '@/test/render';
import { fakeSession, SESSION_PATH, server, TEST_USERNAME } from '@/test/server';

const PROTECTED_TEXT = 'Protected content';
const LOGIN_TEXT = 'Sign-in form';

/**
 * A two-route table: one guarded page and the login page it falls back to.
 * Deliberately not the real route table - this file is about the guard, and a
 * redirect that lands on a stub proves the same thing with less to go wrong.
 */
function renderGuard(initialEntries: readonly string[] = ['/secret']) {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<p>{LOGIN_TEXT}</p>} />
      <Route
        path="/secret"
        element={
          <RequireSession>
            <p>{PROTECTED_TEXT}</p>
          </RequireSession>
        }
      />
    </Routes>,
    initialEntries,
  );
}

describe('RequireSession', () => {
  it('renders the protected route when a session exists', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderGuard();

    // The positive control. Without it, a guard that redirected unconditionally
    // would pass every other test in this file.
    expect(await screen.findByText(PROTECTED_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(LOGIN_TEXT)).not.toBeInTheDocument();
  });

  it('redirects to the login page when the session query resolves to null', async () => {
    // The default handler answers `GET /api/auth/session` with the backend's
    // real 401 problem document. The session query has to turn that into
    // `null`, not into an error: "signed out" is an answer, not a failure.
    renderGuard();

    expect(await screen.findByText(LOGIN_TEXT)).toBeInTheDocument();
    expect(screen.queryByText(PROTECTED_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('renders the skeleton while the session is pending', async () => {
    server.use(
      http.get(SESSION_PATH, async () => {
        await delay(50);
        return HttpResponse.json({ username: TEST_USERNAME });
      }),
    );

    renderGuard();

    // Neither answer is known yet, so neither the protected page nor the login
    // page may be shown. Rendering the redirect first is the flicker this
    // state exists to prevent.
    expect(screen.getByRole('status')).toBeInTheDocument();
    expect(screen.queryByText(LOGIN_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByText(PROTECTED_TEXT)).not.toBeInTheDocument();

    // Let the query settle so the test does not leak a pending request.
    expect(await screen.findByText(PROTECTED_TEXT)).toBeInTheDocument();
  });

  it('renders an error state, not a redirect, when the session cannot be read', async () => {
    server.use(http.get(SESSION_PATH, () => HttpResponse.error()));

    renderGuard();

    // "The backend is unreachable" is not "you are signed out". Sending the
    // owner to a login form they cannot use, over and over, is the failure
    // this branch exists to avoid.
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(LOGIN_TEXT)).not.toBeInTheDocument();
    expect(screen.queryByText(PROTECTED_TEXT)).not.toBeInTheDocument();
  });

  it('treats a 500 from the session endpoint as an error, not as signed out', async () => {
    server.use(
      http.get(SESSION_PATH, () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Internal Server Error',
            status: 500,
            detail: 'The server encountered an unexpected condition.',
          },
          { status: 500, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    renderGuard();

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText(LOGIN_TEXT)).not.toBeInTheDocument();
  });

  it('tells the user the backend is unreachable when a proxy answers for it', async () => {
    server.use(
      http.get(
        SESSION_PATH,
        () =>
          new HttpResponse('<html><body>502 Bad Gateway</body></html>', {
            status: 502,
            statusText: 'Bad Gateway',
            headers: { 'content-type': 'text/html' },
          }),
      ),
    );

    renderGuard();

    // The guard has the same shape of bug as the sign-out alert did: a
    // synthesised problem document whose title is the HTTP reason phrase would
    // otherwise beat the sentence written for exactly this failure.
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not be reached/i);
    expect(alert).not.toHaveTextContent(/bad gateway/i);
    expect(screen.queryByText(LOGIN_TEXT)).not.toBeInTheDocument();
  });

  it('falls back to the problem title when the server sends no detail', async () => {
    // The backend serialises its problem documents with `exclude_none=True`, so
    // `detail` really is absent whenever an error carries no specific message.
    server.use(
      http.get(SESSION_PATH, () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502 },
          { status: 502, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    renderGuard();

    expect(await screen.findByRole('alert')).toHaveTextContent('Bad Gateway');
  });

  it('retries the session read when the error state offers to', async () => {
    const user = userEvent.setup();
    server.use(http.get(SESSION_PATH, () => HttpResponse.error()));

    renderGuard();

    await screen.findByRole('alert');

    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);
    await user.click(screen.getByRole('button', { name: /try again/i }));

    expect(await screen.findByText(PROTECTED_TEXT)).toBeInTheDocument();
  });
});
