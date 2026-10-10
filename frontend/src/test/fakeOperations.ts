/**
 * An in-memory `/api/exchange-operations` for the Operations page's tests (spec 042).
 *
 * It keeps the stored operations, answers the list newest first and a page at a time, and
 * records every request, so a test can assert what the page sent. Uploads answer with the
 * report a test hands it, or with a refusal; writes go through the backend's JSON write guard.
 */
import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  IMPORTS_PATH,
  OPERATIONS_PATH,
  type ImportReport,
  type ManualOperationRequest,
  type Operation,
} from '@/api/operations';

import { operation } from './investmentFixtures';
import { problem, refuseNonJsonWrite } from './server';

export interface RecordedOperationRequest {
  readonly method: string;
  readonly url: string;
  readonly body: unknown;
}

export interface FakeOperationsOptions {
  readonly operations?: readonly Operation[];
  /** What each upload answers, in order; a `Response` is answered as it is. */
  readonly uploads?: readonly (ImportReport | Response)[];
}

export interface FakeOperations {
  readonly handlers: HttpHandler[];
  readonly requests: RecordedOperationRequest[];
}

export function fakeOperations(options: FakeOperationsOptions = {}): FakeOperations {
  let stored = [...(options.operations ?? [])];
  const uploads = [...(options.uploads ?? [])];
  const requests: RecordedOperationRequest[] = [];
  let nextId = 1000;

  async function record(request: Request): Promise<unknown> {
    const text = await request.clone().text();
    const body: unknown = text === '' ? undefined : JSON.parse(text);
    requests.push({ method: request.method, url: request.url, body });
    return body;
  }

  const handlers: HttpHandler[] = [
    http.get(OPERATIONS_PATH, async ({ request }) => {
      await record(request);
      const params = new URL(request.url).searchParams;
      const limit = Number.parseInt(params.get('limit') ?? '100', 10);
      const offset = Number.parseInt(params.get('offset') ?? '0', 10);
      const asset = params.get('asset');
      const venue = params.get('venue');
      const since = params.get('since');
      const until = params.get('until');
      const kept = stored.filter(
        (row) =>
          (asset === null || row.asset === asset) &&
          (venue === null || row.venue === venue) &&
          (since === null || Date.parse(row.executed_at) >= Date.parse(since)) &&
          (until === null || Date.parse(row.executed_at) < Date.parse(until)),
      );
      const newest = kept.sort((a, b) => b.executed_at.localeCompare(a.executed_at));
      return HttpResponse.json({
        count: kept.length,
        operations: newest.slice(offset, offset + limit),
        assets: [...new Set(stored.map((row) => row.asset))].sort(),
        venues: [...new Set(stored.map((row) => row.venue))].sort(),
      });
    }),

    http.post(IMPORTS_PATH, async ({ request }) => {
      await record(request);
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      const next = uploads.shift();
      if (next === undefined) {
        return problem(500, 'Internal Server Error', 'No upload was expected.');
      }
      return next instanceof Response ? next : HttpResponse.json(next, { status: 201 });
    }),

    http.post(OPERATIONS_PATH, async ({ request }) => {
      const body = (await record(request)) as ManualOperationRequest;
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      nextId += 1;
      const created = operation({
        id: nextId,
        source: 'manual',
        venue: body.venue,
        external_id: `manual:${String(nextId)}`,
        executed_at: body.executed_at,
        kind: body.kind,
        asset: body.asset.toUpperCase(),
        quantity: body.quantity,
        quote_currency: body.quote_currency?.toUpperCase() ?? null,
        quote_amount: body.quote_amount ?? null,
        fee_asset: null,
        fee_amount: null,
        description: body.description,
        manual: true,
      });
      stored = [...stored, created];
      return HttpResponse.json(created, { status: 201 });
    }),

    http.delete(`${OPERATIONS_PATH}/:id`, async ({ request, params }) => {
      await record(request);
      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      const id = Number.parseInt(String(params.id), 10);
      const found = stored.find((row) => row.id === id);
      if (found === undefined) {
        return problem(404, 'Not Found', 'No such operation.');
      }
      if (!found.manual) {
        return problem(409, 'Conflict', 'Only a manual entry can be deleted.');
      }
      stored = stored.filter((row) => row.id !== id);
      return new HttpResponse(null, { status: 204 });
    }),
  ];

  return { handlers, requests };
}
