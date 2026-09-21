import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { act, render, screen, type RenderResult } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { App } from '@/App';
import { createQueryClient } from '@/lib/queryClient';

export interface ProvidedRender extends RenderResult {
  readonly queryClient: QueryClient;
}

/**
 * Mounts a tree inside exactly the providers `main.tsx` mounts, `StrictMode`
 * included.
 *
 * `StrictMode` is not decoration here. React double-invokes effects under it,
 * so anything written as an effect-driven redirect fires twice and shows up as
 * a duplicated navigation. The guard is required to be declarative
 * (`<Navigate>`), and this is what would catch it if it stopped being so.
 *
 * The query client is the shipped `createQueryClient()`, never a test-only one,
 * so the retry, cache and `401` behaviour under test is the behaviour that
 * ships.
 */
export function renderWithProviders(
  ui: ReactNode,
  initialEntries: readonly string[] = ['/'],
): ProvidedRender {
  const queryClient = createQueryClient();
  visited.length = 0;

  const result = render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[...initialEntries]}>{ui}</MemoryRouter>
      </QueryClientProvider>
    </StrictMode>,
  );

  return Object.assign(result, { queryClient });
}

const LOCATION_TEST_ID = 'current-location';

/** Every distinct path the router has been on, oldest first. */
const visited: string[] = [];

/**
 * Reports the router's location into the DOM and records where it has been.
 *
 * Asserting on the path directly is what makes "lands on the dashboard" and
 * "refuses a non-local return path" say what they mean. Inferring the
 * destination from the text that happens to be on screen would pass for a
 * redirect that landed somewhere else with similar-looking content.
 *
 * The trail matters as much as the destination. A `waitFor` that waits for a
 * path to *appear* is satisfied by the instant it appears, so a redirect that
 * reaches `/health` and is then overridden a tick later still passes - which
 * is exactly how a real sign-in bug reached the running app while this suite
 * stayed green. {@link visitedPaths} makes the override visible, and
 * {@link settle} is what makes "final" mean final.
 */
function LocationProbe() {
  const location = useLocation();
  const path = `${location.pathname}${location.search}`;

  // Recording during render is a deliberate exception: `StrictMode` renders
  // twice, and de-duplicating consecutive entries makes that harmless, whereas
  // an effect would miss a location the router passed straight through.
  if (visited[visited.length - 1] !== path) {
    visited.push(path);
  }

  return <span data-testid={LOCATION_TEST_ID}>{path}</span>;
}

/** The path the router is currently on. */
export function currentPath(): string {
  return screen.getByTestId(LOCATION_TEST_ID).textContent;
}

/** Every distinct path the router has visited this render, oldest first. */
export function visitedPaths(): readonly string[] {
  return [...visited];
}

/**
 * Lets every queued effect, query resolution and redirect run to completion,
 * so that the location read afterwards is the one the user is left on rather
 * than one the app was passing through.
 */
export async function settle(): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

/**
 * Mounts the whole application shell, route table included.
 *
 * `extra` is rendered inside the router alongside the app, for a test that
 * needs a probe of its own next to the real tree.
 */
export function renderApp(
  initialEntries: readonly string[] = ['/'],
  extra?: ReactNode,
): ProvidedRender {
  return renderWithProviders(
    <>
      <App />
      <LocationProbe />
      {extra}
    </>,
    initialEntries,
  );
}
