import { useMutation, useQueryClient } from '@tanstack/react-query';
import { type SubmitEvent, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';

import { describeApiError } from '@/api/client';
import { login, sessionQueryKey, useSession } from '@/api/session';
import { ErrorState } from '@/components/ErrorState';

const SESSION_UNREACHABLE_MESSAGE =
  'The backend could not be reached. Check that the API is running, then reload the page.';

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
  const queryClient = useQueryClient();
  const session = useSession();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const mutation = useMutation({
    mutationFn: () => login({ username, password }),
    onSuccess: async () => {
      // Only invalidate here - do not also navigate imperatively. The
      // resolved session flips `session.data` truthy, which re-renders this
      // component into the declarative branch below. An imperative
      // `navigate(resolveReturnPath(...))` racing that re-render is what
      // caused a real sign-in to land on `/` instead of the attempted route:
      // the guard's own redirect (previously hardcoded to `/`) could win the
      // race. One redirect, declarative, reading the same `location.state`,
      // removes the race instead of tuning it.
      await queryClient.invalidateQueries({ queryKey: sessionQueryKey });
    },
  });

  if (session.data) {
    return <Navigate to={resolveReturnPath(location.state)} replace />;
  }

  // The credentials were accepted - `mutation` succeeded - but the read that
  // confirms *who* now holds the cookie failed. Without this branch the user
  // is stranded on a login form while already signed in: `session.data` is
  // `undefined`, not `null`, so the redirect above never fires, and nothing
  // said why. `RequireSession` would have shown exactly this after the old,
  // now-removed imperative `navigate()` sent them off this page; this is that
  // same recovery, kept on the page a failed confirmation actually happened.
  if (mutation.isSuccess && session.isError) {
    return (
      <ErrorState
        title="Signed in, but we could not confirm it"
        description={describeApiError(session.error, SESSION_UNREACHABLE_MESSAGE)}
        onRetry={() => {
          void session.refetch();
        }}
      />
    );
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
          <ErrorState
            title="Sign-in failed"
            description={describeApiError(
              mutation.error,
              'The server could not be reached. Check your connection and try again.',
            )}
          />
        )}

        <button type="submit" disabled={mutation.isPending}>
          Sign in
        </button>
      </form>
    </section>
  );
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
