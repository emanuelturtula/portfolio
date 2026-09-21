import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { render, screen, type RenderResult } from '@testing-library/react';
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

/**
 * Reports the router's location into the DOM.
 *
 * Asserting on the path directly is what makes "lands on the dashboard" and
 * "refuses a non-local return path" say what they mean. Inferring the
 * destination from the text that happens to be on screen would pass for a
 * redirect that landed somewhere else with similar-looking content.
 */
function LocationProbe() {
  const location = useLocation();

  return <span data-testid={LOCATION_TEST_ID}>{`${location.pathname}${location.search}`}</span>;
}

/** The path the router is currently on. */
export function currentPath(): string {
  return screen.getByTestId(LOCATION_TEST_ID).textContent;
}

/** Mounts the whole application shell, route table included. */
export function renderApp(initialEntries: readonly string[] = ['/']): ProvidedRender {
  return renderWithProviders(
    <>
      <App />
      <LocationProbe />
    </>,
    initialEntries,
  );
}
