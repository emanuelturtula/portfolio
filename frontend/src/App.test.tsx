import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';

import { threeAdjustments, threeFirstTrades } from '@/test/adjustmentFixtures';
import { exchange } from '@/test/exchangeFixtures';
import { fakeAccounting } from '@/test/fakeAccounting';
import { fakeAdjustments } from '@/test/fakeAdjustments';
import { fakeExchanges } from '@/test/fakeExchanges';
import { fakePortfolio, recordRequestUrls } from '@/test/fakePortfolio';
import { currentPath, renderApp, settle, visitedPaths } from '@/test/render';
import {
  fakeSession,
  HEALTH_PATH,
  LOGOUT_PATH,
  server,
  TEST_PASSWORD,
  TEST_USERNAME,
  unauthorized,
} from '@/test/server';

/** True while the login form is on screen. */
function loginFormIsShown(): boolean {
  return screen.queryByLabelText(/password/i) !== null;
}

beforeEach(() => {
  // The dashboard reads the portfolio as soon as a session exists. An empty
  // one is the first-time owner; tests that need data register their own.
  // The exchanges page likewise, with no exchange configured, the dashboard's invested
  // section with a snapshot over no events, and the adjustments page with none recorded.
  server.use(
    ...fakePortfolio().handlers,
    ...fakeExchanges().handlers,
    ...fakeAccounting().handlers,
    ...fakeAdjustments().handlers,
  );
});

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

  it('renders the dashboard empty state rather than a blank page', async () => {
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers);

    renderApp(['/']);

    const main = await screen.findByRole('main');
    expect(
      await within(main).findByRole('heading', { name: /no wallets yet/i }),
    ).toBeInTheDocument();
    // The invested section below settles to its own empty state; until it has, its skeleton
    // is a `status` of its own, and the assertion below would be about a race.
    expect(
      await within(main).findByRole('heading', { name: 'No trades imported yet' }),
    ).toBeInTheDocument();
    // An empty state, not a failure and not a permanent loading state.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('the header links to the dashboard, the wallets, the exchanges and the adjustments', async () => {
    const user = userEvent.setup();
    server.use(
      ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
      ...fakeExchanges({ exchanges: [exchange()] }).handlers,
    );

    renderApp(['/']);

    const nav = await screen.findByRole('navigation', { name: 'Main' });
    const dashboard = within(nav).getByRole('link', { name: 'Dashboard' });
    const wallets = within(nav).getByRole('link', { name: 'Wallets' });
    const exchanges = within(nav).getByRole('link', { name: 'Exchanges' });
    const adjustments = within(nav).getByRole('link', { name: 'Adjustments' });
    expect(dashboard).toHaveAttribute('href', '/');
    expect(wallets).toHaveAttribute('href', '/wallets');
    expect(exchanges).toHaveAttribute('href', '/exchanges');
    // The bare page: the link carries no asset, so the form it opens is empty.
    expect(adjustments).toHaveAttribute('href', '/adjustments');
    // In that order: Exchanges comes after Wallets, and Adjustments is the fourth (spec 027).
    expect(
      within(nav)
        .getAllByRole('link')
        .map((link) => link.textContent),
    ).toEqual(['Dashboard', 'Wallets', 'Exchanges', 'Adjustments']);
    // The current page is marked for assistive technology, not by colour alone.
    expect(dashboard).toHaveAttribute('aria-current', 'page');
    expect(wallets).not.toHaveAttribute('aria-current');
    expect(exchanges).not.toHaveAttribute('aria-current');
    expect(adjustments).not.toHaveAttribute('aria-current');

    await user.click(wallets);

    expect(await screen.findByRole('form', { name: 'Add a wallet' })).toBeInTheDocument();
    expect(currentPath()).toBe('/wallets');
    expect(within(nav).getByRole('link', { name: 'Wallets' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    // `end` on the dashboard link: `/` must not also match `/wallets`.
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'aria-current',
    );

    await user.click(within(nav).getByRole('link', { name: 'Exchanges' }));

    expect(await screen.findByRole('listitem', { name: 'Bitget' })).toBeInTheDocument();
    expect(currentPath()).toBe('/exchanges');
    expect(within(nav).getByRole('link', { name: 'Exchanges' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).getByRole('link', { name: 'Wallets' })).not.toHaveAttribute('aria-current');
    expect(within(nav).getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute(
      'aria-current',
    );

    await user.click(within(nav).getByRole('link', { name: 'Adjustments' }));

    expect(await screen.findByRole('form', { name: 'Record an adjustment' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Adjustments' })).toBeInTheDocument();
    expect(currentPath()).toBe('/adjustments');
    expect(within(nav).getByRole('link', { name: 'Adjustments' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    for (const other of ['Dashboard', 'Wallets', 'Exchanges']) {
      expect(within(nav).getByRole('link', { name: other })).not.toHaveAttribute('aria-current');
    }

    await user.click(within(nav).getByRole('link', { name: 'Dashboard' }));

    expect(await screen.findByRole('heading', { name: /no wallets yet/i })).toBeInTheDocument();
    expect(currentPath()).toBe('/');
    expect(within(nav).getByRole('link', { name: 'Adjustments' })).not.toHaveAttribute(
      'aria-current',
    );
  });

  it('shows no navigation while signed out', async () => {
    renderApp(['/login']);

    await screen.findByLabelText(/username/i);
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Wallets' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Exchanges' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Adjustments' })).not.toBeInTheDocument();
  });

  it('/wallets requires a session', async () => {
    const urls = recordRequestUrls();

    renderApp(['/wallets']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
    expect(loginFormIsShown()).toBe(true);
    // The guard decides before the page mounts: nothing about wallets was
    // even asked for.
    expect(urls.filter((url) => new URL(url).pathname.startsWith('/api/wallets'))).toEqual([]);
    expect(screen.queryByRole('form', { name: 'Add a wallet' })).not.toBeInTheDocument();
  });

  it('/exchanges requires a session', async () => {
    const urls = recordRequestUrls();

    renderApp(['/exchanges']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
    expect(loginFormIsShown()).toBe(true);
    // The guard decides before the page mounts: nothing about exchanges was
    // even asked for.
    expect(urls.filter((url) => new URL(url).pathname.startsWith('/api/exchanges'))).toEqual([]);
    expect(screen.queryByRole('heading', { name: 'Exchanges' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
  });

  it('/adjustments requires a session', async () => {
    // Spec 027, criterion 5. Signed out, with adjustments on the server that must not be read.
    const adjustments = fakeAdjustments({
      adjustments: threeAdjustments(),
      firstTrades: threeFirstTrades(),
    });
    server.use(...adjustments.handlers);
    const urls = recordRequestUrls();

    renderApp(['/adjustments?asset=BTC']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
    expect(loginFormIsShown()).toBe(true);
    // The guard decides before the page mounts: nothing about adjustments, first trades or
    // positions was even asked for.
    expect(urls.filter((url) => new URL(url).pathname.startsWith('/api/accounting'))).toEqual([]);
    expect(adjustments.requests).toEqual([]);
    expect(screen.queryByRole('heading', { name: 'Adjustments' })).not.toBeInTheDocument();
    expect(screen.queryByRole('form', { name: 'Record an adjustment' })).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    expect(screen.queryByText('BTC')).not.toBeInTheDocument();
  });

  it('returns to /adjustments, with its asset, after signing in from a redirect', async () => {
    const user = userEvent.setup();
    server.use(
      ...fakeSession().handlers,
      ...fakeAdjustments({ adjustments: threeAdjustments(), firstTrades: threeFirstTrades() })
        .handlers,
    );

    renderApp(['/adjustments?asset=KAS']);
    await user.type(await screen.findByLabelText(/username/i), TEST_USERNAME);
    await user.type(screen.getByLabelText(/password/i), TEST_PASSWORD);
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    const form = await screen.findByRole('form', { name: 'Record an adjustment' });
    await settle();
    expect(currentPath()).toBe('/adjustments?asset=KAS');
    // The link from the holdings check survives the detour through the login page.
    expect(within(form).getByLabelText('Asset')).toHaveValue('KAS');
    expect(await screen.findByRole('rowheader', { name: 'KAS' })).toBeInTheDocument();
  });

  it('returns to /exchanges after signing in from a redirect', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers, ...fakeExchanges({ exchanges: [exchange()] }).handlers);

    renderApp(['/exchanges']);
    await user.type(await screen.findByLabelText(/username/i), TEST_USERNAME);
    await user.type(screen.getByLabelText(/password/i), TEST_PASSWORD);
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByRole('listitem', { name: 'Bitget' })).toBeInTheDocument();
    await settle();
    expect(currentPath()).toBe('/exchanges');
  });

  it('returns to /wallets after signing in from a redirect', async () => {
    const user = userEvent.setup();
    server.use(...fakeSession().handlers);

    renderApp(['/wallets']);
    await user.type(await screen.findByLabelText(/username/i), TEST_USERNAME);
    await user.type(screen.getByLabelText(/password/i), TEST_PASSWORD);
    await user.click(screen.getByRole('button', { name: /sign in/i }));

    expect(await screen.findByRole('form', { name: 'Add a wallet' })).toBeInTheDocument();
    await settle();
    expect(currentPath()).toBe('/wallets');
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
      expect(loginFormIsShown()).toBe(true);
    });
    await settle();

    // The settled location. A bare waitFor on the path is satisfied by the
    // first instant it matches, which is how the sign-in race stayed hidden.
    expect(currentPath()).toBe('/login');
    expect(visitedPaths().at(-1)).toBe('/login');
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
      expect(loginFormIsShown()).toBe(true);
    });
    await settle();

    expect(currentPath()).toBe('/login');
    expect(visitedPaths().at(-1)).toBe('/login');
    expect(fake.currentUser()).toBeNull();

    // `POST /api/auth/logout` carries no body. The backend's write guard still
    // requires `Content-Type: application/json` on it, and the handler in
    // `src/test/server.ts` refuses the request with a 403 when it is missing -
    // so setting the header only when there is a body fails right here.
    expect(fake.logouts).toHaveLength(1);
    expect(fake.logouts[0]?.contentType).toBe('application/json');
  });

  it('warns that the session may still be active when the sign-out cannot be sent', async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);
    server.use(http.post(LOGOUT_PATH, () => HttpResponse.error()));

    renderApp(['/']);
    await user.click(await screen.findByRole('button', { name: /sign out/i }));

    const alert = await screen.findByRole('alert');
    // A network failure carries no problem document, so this is the one case
    // where the page supplies the sentence - and the sentence has to say the
    // session may still be live, because it is.
    expect(alert).toHaveTextContent(/could not reach the server to sign you out/i);
    expect(alert).toHaveTextContent(/session may still be active/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('says the session may still be active when a proxy answers instead of the backend', async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);
    // The live-run case: the backend is down, Vite's proxy answers the write
    // with an HTML 502, and the reason phrase "Bad Gateway" reached the user
    // instead of the sentence this branch exists to show.
    server.use(
      http.post(
        LOGOUT_PATH,
        () =>
          new HttpResponse('<html><body>502 Bad Gateway</body></html>', {
            status: 502,
            statusText: 'Bad Gateway',
            headers: { 'content-type': 'text/html' },
          }),
      ),
    );

    renderApp(['/']);
    await user.click(await screen.findByRole('button', { name: /sign out/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not reach the server to sign you out/i);
    expect(alert).toHaveTextContent(/session may still be active/i);
    expect(alert).not.toHaveTextContent(/bad gateway/i);
    // And the warning is true: the cookie really is still live.
    expect(fake.currentUser()).toBe(TEST_USERNAME);
  });

  it("shows the server's own message when the sign-out is refused", async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);
    server.use(
      http.post(LOGOUT_PATH, () =>
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

    renderApp(['/']);
    await user.click(await screen.findByRole('button', { name: /sign out/i }));

    // When the backend did answer, its sentence wins over the local fallback -
    // the same rule the login page and the health page follow.
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The server encountered an unexpected condition.');
  });

  it('keeps the user signed in when the sign-out fails', async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);
    server.use(http.post(LOGOUT_PATH, () => HttpResponse.error()));

    renderApp(['/']);
    await user.click(await screen.findByRole('button', { name: /sign out/i }));
    await screen.findByRole('alert');
    await settle();

    // The session cookie is still valid on the server, so the honest thing is
    // to stay put. Redirecting to `/login` would tell the user they had signed
    // out while the session that matters carried on living - the one failure
    // mode worse than showing an error.
    expect(fake.currentUser()).toBe(TEST_USERNAME);
    expect(currentPath()).toBe('/');
    expect(visitedPaths().at(-1)).toBe('/');
    expect(loginFormIsShown()).toBe(false);
    expect(screen.getByText(TEST_USERNAME)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /sign out/i })).toBeInTheDocument();
  });

  it('signs out on a second attempt after a failed one', async () => {
    const user = userEvent.setup();
    const fake = fakeSession({ initialUser: TEST_USERNAME });
    server.use(...fake.handlers);
    server.use(http.post(LOGOUT_PATH, () => HttpResponse.error()));

    renderApp(['/']);
    await user.click(await screen.findByRole('button', { name: /sign out/i }));
    await screen.findByRole('alert');

    // The button has to stay usable: a failed sign-out the user cannot retry
    // is a session they cannot end.
    server.use(...fake.handlers);
    await user.click(screen.getByRole('button', { name: /sign out/i }));

    await waitFor(() => {
      expect(loginFormIsShown()).toBe(true);
    });
    await settle();
    expect(currentPath()).toBe('/login');
    expect(fake.currentUser()).toBeNull();
  });

  it('offers no sign-out control while signed out', async () => {
    renderApp(['/login']);

    await screen.findByLabelText(/username/i);
    expect(screen.queryByRole('button', { name: /sign out/i })).not.toBeInTheDocument();
  });
});
