import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  accountSucceeded,
  assertWritableExchange,
  exchangeList,
  finishedRun,
  syncTriggered,
  type ExchangeResponse,
  type ExchangeSyncRunResponse,
  type ExchangeSyncTriggeredResponse,
} from './exchangeFixtures';
import type { RecordedRequest } from './fakePortfolio';
import { refuseNonJsonWrite, unauthorized } from './server';

export const EXCHANGES_PATH = '/api/exchanges';
export const EXCHANGE_RUNS_PATH = '/api/exchanges/runs';
export const EXCHANGE_SYNC_PATH = '/api/exchanges/sync';

/** The backend's default page size for the run log, and its ceiling. */
const DEFAULT_RUNS_LIMIT = 20;
const MAX_RUNS_LIMIT = 100;

/** A route a test can hold open, or make fail, to observe the page meanwhile. */
export type ExchangeRoute = 'list' | 'runs' | 'sync';

const ROUTE_PATHS: Readonly<Record<ExchangeRoute, string>> = {
  list: EXCHANGES_PATH,
  runs: EXCHANGE_RUNS_PATH,
  sync: EXCHANGE_SYNC_PATH,
};

export interface FakeExchangesOptions {
  readonly exchanges?: readonly ExchangeResponse[];
  readonly runs?: readonly ExchangeSyncRunResponse[];
  /**
   * What `POST /api/exchanges/sync` does once released. Receives the fake so
   * it can move the list and the run log the way a real run would. The
   * default records a manual run that succeeded for every configured venue,
   * puts it at the head of the run log, and answers with it, unjoined.
   */
  readonly onSync?: (fake: FakeExchanges) => ExchangeSyncTriggeredResponse;
  /**
   * The session these endpoints belong to. While it is signed out, every
   * request is answered `401`, as the backend's middleware answers it.
   */
  readonly session?: { currentUser(): string | null };
}

export interface FakeExchanges {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  /** Every request any of these handlers saw, oldest first, recorded on arrival. */
  readonly requests: RecordedRequest[];
  exchanges(): readonly ExchangeResponse[];
  runs(): readonly ExchangeSyncRunResponse[];
  /** Replaces the list, as the server's state changing with no request from the page. */
  setExchanges(next: readonly ExchangeResponse[]): void;
  /** Replaces one venue's entry, checked against the backend's writer rules. */
  patchExchange(key: ExchangeResponse['exchange_key'], patch: Partial<ExchangeResponse>): void;
  setRuns(next: readonly ExchangeSyncRunResponse[]): void;
  /**
   * Holds every request to `route` until the returned function is called. The
   * request is recorded when it arrives, not when it is released.
   */
  hold(route: ExchangeRoute): () => void;
  /**
   * Answers every request to `route` with `respond()` until called again with
   * `null`. The request is still recorded.
   */
  fail(route: ExchangeRoute, respond: (() => Response) | null): void;
  /** How many requests reached `route`, including held and failed ones. */
  count(route: ExchangeRoute): number;
  /** The requests that reached `route`. */
  requestsTo(route: ExchangeRoute): RecordedRequest[];
}

/**
 * A stateful fake of the three exchange endpoints.
 *
 * The list and the run log are held as state, so a test changes them the way
 * a sync on the server would and the page's next read - a poll, or the
 * refetch a sync's settle triggers - sees the change. The write goes through
 * {@link refuseNonJsonWrite}, the backend's guard, so a bodyless `POST` that
 * stops declaring JSON fails here rather than on the Pi.
 */
export function fakeExchanges(options: FakeExchangesOptions = {}): FakeExchanges {
  // Entries are checked as they are, not copied: a fixture is never mutated, and a
  // copy would lose the identity an opt-in such as drainedBeforeMarkSynced rides on.
  let exchanges: ExchangeResponse[] = (options.exchanges ?? []).map((entry) =>
    assertWritableExchange(entry),
  );
  let runs: ExchangeSyncRunResponse[] = [...(options.runs ?? [])];
  const holds = new Map<ExchangeRoute, Promise<void>>();
  const failures = new Map<ExchangeRoute, () => Response>();
  const requests: RecordedRequest[] = [];

  async function record(request: Request): Promise<void> {
    let body: unknown;
    try {
      body = (await request.clone().json()) as unknown;
    } catch {
      body = undefined;
    }
    requests.push({
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body,
    });
  }

  function routeOf(entry: RecordedRequest): ExchangeRoute | undefined {
    const path = new URL(entry.url).pathname;
    return (Object.keys(ROUTE_PATHS) as ExchangeRoute[]).find(
      (route) => ROUTE_PATHS[route] === path,
    );
  }

  function defaultSync(): ExchangeSyncTriggeredResponse {
    const nextId = runs.reduce((max, run) => Math.max(max, run.run_id), 0) + 1;
    const run = finishedRun({
      run_id: nextId,
      trigger: 'manual',
      started_at: new Date(Date.now()).toISOString(),
      accounts: exchanges
        .filter((entry) => entry.configured)
        .map((entry) => accountSucceeded(entry.exchange_key)),
    });
    runs = [run, ...runs];
    return syncTriggered(run, false);
  }

  const fake: FakeExchanges = {
    handlers: [],
    requests,
    exchanges: () => exchanges,
    runs: () => runs,
    setExchanges: (next) => {
      exchanges = exchangeList(...next.map((entry) => assertWritableExchange(entry))).exchanges;
    },
    patchExchange: (key, patch) => {
      exchanges = exchanges.map((entry) =>
        entry.exchange_key === key ? assertWritableExchange({ ...entry, ...patch }) : entry,
      );
    },
    setRuns: (next) => {
      runs = [...next];
    },
    hold: (route) => {
      let release: () => void = () => undefined;
      holds.set(
        route,
        new Promise<void>((resolve) => {
          release = resolve;
        }),
      );
      return () => {
        holds.delete(route);
        release();
      };
    },
    fail: (route, respond) => {
      if (respond === null) {
        failures.delete(route);
      } else {
        failures.set(route, respond);
      }
    },
    count: (route) => requests.filter((entry) => routeOf(entry) === route).length,
    requestsTo: (route) => requests.filter((entry) => routeOf(entry) === route),
  };

  /** Records, waits out a hold, then answers a staged failure if there is one. */
  async function arrive(route: ExchangeRoute, request: Request): Promise<Response | undefined> {
    await record(request);
    await holds.get(route);
    return failures.get(route)?.();
  }

  const requireSession = async ({ request }: { request: Request }) => {
    if (options.session?.currentUser() !== null) {
      return undefined;
    }
    await record(request);
    return unauthorized();
  };

  fake.handlers.push(
    http.all(EXCHANGES_PATH, requireSession),
    http.all(`${EXCHANGES_PATH}/*`, requireSession),

    http.get(EXCHANGES_PATH, async ({ request }) => {
      const failure = await arrive('list', request);
      return failure ?? HttpResponse.json(exchangeList(...exchanges));
    }),

    http.get(EXCHANGE_RUNS_PATH, async ({ request }) => {
      const failure = await arrive('runs', request);
      if (failure !== undefined) {
        return failure;
      }
      const raw = new URL(request.url).searchParams.get('limit');
      const limit = raw === null ? DEFAULT_RUNS_LIMIT : Number.parseInt(raw, 10);
      const bounded = Math.max(1, Math.min(limit, MAX_RUNS_LIMIT));
      return HttpResponse.json({ runs: runs.slice(0, bounded) });
    }),

    http.post(EXCHANGE_SYNC_PATH, async ({ request }) => {
      const failure = await arrive('sync', request);

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }
      if (failure !== undefined) {
        return failure;
      }

      return HttpResponse.json(options.onSync !== undefined ? options.onSync(fake) : defaultSync());
    }),
  );

  return fake;
}
