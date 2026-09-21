import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Route, Routes, useNavigate } from 'react-router-dom';

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
 * its own, because `RequireSession` already owns telling the user their
 * session could not be read.
 */
function AccountControls() {
  const session = useSession();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const mutation = useMutation({
    mutationFn: logout,
    onSuccess: () => {
      queryClient.setQueryData(sessionQueryKey, null);
      void navigate('/login', { replace: true });
    },
  });

  if (!session.data) {
    return null;
  }

  return (
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
  );
}
