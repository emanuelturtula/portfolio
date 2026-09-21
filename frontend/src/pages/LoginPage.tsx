import { useMutation, useQueryClient } from '@tanstack/react-query';
import { type SubmitEvent, useState } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';

import { ApiError } from '@/api/client';
import { login, sessionQueryKey, useSession } from '@/api/session';
import { ErrorState } from '@/components/ErrorState';

/**
 * `POST /api/auth/login` form.
 *
 * Redirects away when a session already exists, so the back button never
 * lands on a login form the user cannot leave - rendered declaratively as
 * `<Navigate>`, not from an effect, for the same `StrictMode` double-invoke
 * reason `RequireSession` avoids one.
 *
 * On success, returns to the location `RequireSession` captured before
 * redirecting here, defaulting to `/`. Only a same-origin path is honoured:
 * open redirects through router state are cheap to prevent and expensive to
 * notice later.
 */
export function LoginPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const session = useSession();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const mutation = useMutation({
    mutationFn: () => login({ username, password }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey });
      void navigate(resolveReturnPath(location.state), { replace: true });
    },
  });

  if (session.data) {
    return <Navigate to="/" replace />;
  }

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    mutation.mutate();
  }

  return (
    <section aria-labelledby="login-heading" className="login">
      <h2 id="login-heading">Sign in</h2>
      <form onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            name="username"
            type="text"
            autoComplete="username"
            required
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => {
              setPassword(event.target.value);
            }}
          />
        </div>

        {mutation.isPending && (
          <p className="state" role="status">
            Signing in…
          </p>
        )}

        {mutation.isError && (
          <ErrorState title="Sign-in failed" description={describeLoginError(mutation.error)} />
        )}

        <button type="submit" disabled={mutation.isPending}>
          Sign in
        </button>
      </form>
    </section>
  );
}

/**
 * Turns a failed login attempt into one sentence a user can read.
 *
 * The backend already writes the sentence a person should see - "the
 * username or password is incorrect", "too many failed attempts" - into the
 * problem document's `detail`. Inventing a second wording here would fork the
 * message from the one source of truth for it, so an `ApiError` is always
 * shown as the server wrote it.
 */
function describeLoginError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.problem.detail ?? error.problem.title;
  }

  return 'The server could not be reached. Check your connection and try again.';
}

/**
 * A path is treated as cross-origin, and rejected, when its second character
 * is `/` or `\`. Both are protocol-relative as far as a browser's URL parser
 * is concerned: `//evil.example` is the obvious spelling, but a browser also
 * normalises a leading backslash to a forward slash while parsing a URL, so
 * `/\evil.example` reaches the same other-origin destination.
 */
const OPEN_REDIRECT_PREFIX = /^\/[/\\]/;

/**
 * Reads the location `RequireSession` recorded before redirecting to login,
 * accepting only a same-origin absolute path. Anything else - including a
 * protocol-relative path in either of its spellings, see
 * {@link OPEN_REDIRECT_PREFIX} - is discarded in favour of `/`.
 */
function resolveReturnPath(state: unknown): string {
  if (typeof state === 'object' && state !== null && 'from' in state) {
    const { from } = state as { from?: unknown };

    if (typeof from === 'string' && from.startsWith('/') && !OPEN_REDIRECT_PREFIX.test(from)) {
      return from;
    }
  }

  return '/';
}
