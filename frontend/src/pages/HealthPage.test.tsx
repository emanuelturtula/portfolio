import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { createQueryClient } from '@/lib/queryClient';
import { HealthPage } from '@/pages/HealthPage';
import { server } from '@/test/server';

function renderHealthPage() {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <HealthPage />
    </QueryClientProvider>,
  );
}

describe('HealthPage', () => {
  it('announces that it is loading while the request is in flight', async () => {
    server.use(
      http.get('/api/health', async () => {
        await delay(50);
        return HttpResponse.json({ status: 'ok', version: '0.1.0', environment: 'test' });
      }),
    );

    renderHealthPage();

    const status = screen.getByRole('status');
    expect(status).toHaveTextContent(/loading backend health/i);

    // Let the query settle so the test does not leak a pending request.
    expect(await screen.findByText('ok')).toBeInTheDocument();
  });

  it('renders the status, version and environment on success', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.json({ status: 'ok', version: '1.4.2', environment: 'development' }),
      ),
    );

    renderHealthPage();

    expect(await screen.findByText('ok')).toBeInTheDocument();
    expect(screen.getByText('1.4.2')).toBeInTheDocument();
    expect(screen.getByText('development')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('falls back to the problem title when the server sends no detail', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.json(
          { type: 'about:blank', title: 'Bad Gateway', status: 502 },
          { status: 502, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    renderHealthPage();

    // The backend serialises with `exclude_none=True`, so `detail` really is
    // absent whenever an error carries no specific message.
    expect(await screen.findByRole('alert')).toHaveTextContent('Bad Gateway');
  });

  it('shows an unreachable-backend message when the request fails outright', async () => {
    server.use(http.get('/api/health', () => HttpResponse.error()));

    renderHealthPage();

    const alert = await screen.findByRole('alert');
    // A `TypeError` from `fetch` carries no problem document, so the page needs
    // a sentence of its own. "Failed to fetch" is not one.
    expect(alert).toHaveTextContent(/could not be reached/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('shows an accessible error message when the backend reports a problem', async () => {
    server.use(
      http.get('/api/health', () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Service Unavailable',
            status: 503,
            detail: 'The database is not reachable.',
          },
          { status: 503, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    );

    renderHealthPage();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('The backend health check failed');
    expect(alert).toHaveTextContent('The database is not reachable.');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
