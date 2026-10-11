/**
 * An in-memory `/api/portfolios` for spec 044's tests: list, create, rename and delete, with
 * the backend's refusals (a taken name is a `409`, and so is deleting the last portfolio or
 * one that holds anything). Writes go through the backend's JSON write guard, and every
 * request is recorded.
 */
import { http, HttpResponse, type HttpHandler } from 'msw';

import { PORTFOLIOS_PATH, type Portfolio } from '@/api/portfolios';

import { problem, refuseNonJsonWrite } from './server';

/**
 * The sentences the backend answers with, copied from
 * `backend/src/portfolio/services/portfolios.py`.
 */
export const NAME_TAKEN_DETAIL = 'A portfolio with this name already exists.';
export const LAST_PORTFOLIO_DETAIL = 'The only portfolio cannot be deleted.';

export interface RecordedPortfolioRequest {
  readonly method: string;
  readonly url: string;
  readonly body: unknown;
}

export interface FakePortfolios {
  readonly handlers: HttpHandler[];
  readonly requests: RecordedPortfolioRequest[];
  list(): readonly Portfolio[];
}

/** A portfolio as the backend serves it; empty unless the test says otherwise. */
export function portfolio(
  overrides: Partial<Portfolio> & Pick<Portfolio, 'id' | 'name'>,
): Portfolio {
  return { wallets: 0, operations: 0, created_at: '2026-01-01T00:00:00Z', ...overrides };
}

export function fakePortfolios(initial: readonly Portfolio[]): FakePortfolios {
  let stored = initial.map((row) => ({ ...row }));
  const requests: RecordedPortfolioRequest[] = [];

  async function record(request: Request): Promise<unknown> {
    const text = await request.clone().text();
    const body: unknown = text === '' ? undefined : JSON.parse(text);
    requests.push({ method: request.method, url: request.url, body });
    return body;
  }

  function taken(name: string, except?: number): boolean {
    return stored.some((row) => row.name === name && row.id !== except);
  }

  const handlers: HttpHandler[] = [
    http.get(PORTFOLIOS_PATH, async ({ request }) => {
      await record(request);
      return HttpResponse.json({ portfolios: stored });
    }),

    http.post(PORTFOLIOS_PATH, async ({ request }) => {
      const body = (await record(request)) as { name: string };
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      const name = body.name.trim();
      if (taken(name)) {
        return problem(409, 'Conflict', NAME_TAKEN_DETAIL);
      }
      const created = portfolio({
        id: stored.reduce((max, row) => Math.max(max, row.id), 0) + 1,
        name,
      });
      stored = [...stored, created];
      return HttpResponse.json(created, { status: 201 });
    }),

    http.patch(`${PORTFOLIOS_PATH}/:id`, async ({ request, params }) => {
      const body = (await record(request)) as { name: string };
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      const found = stored.find((row) => String(row.id) === String(params.id));
      if (found === undefined) {
        return problem(404, 'Not Found', 'No portfolio with that id.');
      }
      const name = body.name.trim();
      if (taken(name, found.id)) {
        return problem(409, 'Conflict', NAME_TAKEN_DETAIL);
      }
      const renamed = { ...found, name };
      stored = stored.map((row) => (row.id === found.id ? renamed : row));
      return HttpResponse.json(renamed);
    }),

    http.delete(`${PORTFOLIOS_PATH}/:id`, async ({ request, params }) => {
      await record(request);
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      const found = stored.find((row) => String(row.id) === String(params.id));
      if (found === undefined) {
        return problem(404, 'Not Found', 'No portfolio with that id.');
      }
      if (stored.length === 1) {
        return problem(409, 'Conflict', LAST_PORTFOLIO_DETAIL);
      }
      stored = stored.filter((row) => row.id !== found.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ];

  return { handlers, requests, list: () => stored };
}
