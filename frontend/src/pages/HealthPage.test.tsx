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
