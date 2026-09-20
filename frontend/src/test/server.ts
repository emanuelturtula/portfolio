import { http, HttpResponse } from 'msw';
import { setupServer } from 'msw/node';

/** Body the default `GET /api/health` handler answers with. */
export const healthFixture = {
  status: 'ok',
  version: '0.1.0',
  environment: 'test',
};

export const handlers = [http.get('/api/health', () => HttpResponse.json(healthFixture))];

/**
 * Request interception for the whole test run. Individual tests override a
 * route with `server.use(...)`; `setup.ts` resets those overrides after each
 * test so they never leak.
 */
export const server = setupServer(...handlers);
