import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Route, Routes } from 'react-router-dom';

import { describeApiError } from '@/api/client';
import { logout, sessionQueryKey, useSession } from '@/api/session';
import { RequireSession } from '@/components/RequireSession';
import { DashboardPage } from '@/pages/DashboardPage';
import { HealthPage } from '@/pages/HealthPage';
import { LoginPage } from '@/pages/LoginPage';
import { NotFoundPage } from '@/pages/NotFoundPage';

/**
 * Application shell: a header plus the route table. The router itself lives in
 * `main.tsx` so that tests can mount this component inside a memory router.
 *
 * `/login` is the only public route. Everything else - including the
 * catch-all - is wrapped in `RequireSession`, per the route table in
 * docs/specs/004-login-page-and-app-shell.md.
 */
export function App() {
  return (
    <div className="app">
      <header className="app-header">
        <h1>Portfolio</h1>
        <AccountControls />
      </header>
      <main className="app-main">
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/"
            element={
              <RequireSession>
                <DashboardPage />
              </RequireSession>
            }
          />
          <Route
            path="/health"
            element={
              <RequireSession>
                <HealthPage />
              </RequireSession>
            }
          />
          <Route
            path="*"
            element={
              <RequireSession>
                <NotFoundPage />
              </RequireSession>
            }
          />
        </Routes>
      </main>
    </div>
  );
}

/**
 * Signed-in username and a logout button. Renders nothing while the session
 * is pending, unreachable or signed-out - the header has no error state of
 * its own for *that*, because `RequireSession` already owns telling the user
 * their session could not be read. A failed *sign-out* is different and is
 * this component's own to surface: the session cookie is still valid when
 * that happens, so silence here would leave the user believing they signed
 * out when they did not.
 */
function AccountControls() {
  const session = useSession();
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      // The session entry is set to `null` first, on the query instance an
      // already-mounted `RequireSession` is watching, so its declarative
      // redirect fires immediately. Everything else fetched under the old
      // session is then dropped by key, not wiped wholesale with `clear()`:
      // `clear()` destroys and lazily recreates the query cache entry, and an
      // observer already subscribed to the old instance does not pick up the
      // new one in time for this render - the redirect would then wait on a
      // refetch instead of firing immediately. Excluding the session key from
      // the sweep is what keeps "drop stale data" from undoing "sign out
      // works" a line above it.
      //
      // No imperative `navigate('/login')` alongside this: that was a second
      // redirect mechanism racing the guard's declarative one, the same
      // construction that sent a real sign-in to the wrong route. It happened
      // to be harmless here only because both targeted `/login` - the guard's
      // redirect also carries `state.from`, which the imperative call did
      // not, and jsdom resolves the race in the opposite order from a real
      // browser, so a passing test here proves nothing about either.
      queryClient.setQueryData(sessionQueryKey, null);
      queryClient.removeQueries({
        predicate: (query) => query.queryKey[0] !== sessionQueryKey[0],
      });
    },
  });

  if (!session.data) {
    return null;
  }

  return (
    <div className="account">
      <div className="account-controls">
        <span>{session.data.username}</span>
        <button
          type="button"
          onClick={() => {
            mutation.mutate();
          }}
          disabled={mutation.isPending}
        >
          Sign out
        </button>
      </div>
      {mutation.isError && (
        <p className="state state-error" role="alert">
          {describeApiError(
            mutation.error,
            'Could not reach the server to sign you out. Your session may still be active.',
          )}
        </p>
      )}
    </div>
  );
}
