import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  accountSucceeded,
  ALL_EXCHANGE_KEYS,
  assertWritableExchange,
  exchangeList,
  finishedRun,
  syncTriggered,
  type ExchangeKey,
  type ExchangeResponse,
  type ExchangeSyncRunResponse,
  type ExchangeSyncTriggeredResponse,
} from './exchangeFixtures';
import type { RecordedRequest } from './fakePortfolio';
import {
  assertWritableFills,
  DEFAULT_FILLS_LIMIT,
  fillsPage,
  MAX_FILLS_LIMIT,
  type ExchangeFill,
  type FillQuery,
} from './fillFixtures';
import { problem, refuseNonJsonWrite, unauthorized } from './server';

export const EXCHANGES_PATH = '/api/exchanges';
export const EXCHANGE_RUNS_PATH = '/api/exchanges/runs';
export const EXCHANGE_SYNC_PATH = '/api/exchanges/sync';
export const EXCHANGE_FILLS_PATH = '/api/exchanges/fills';

/** The backend's default page size for the run log, and its ceiling. */
const DEFAULT_RUNS_LIMIT = 20;
const MAX_RUNS_LIMIT = 100;

/** A route a test can hold open, or make fail, to observe the page meanwhile. */
export type ExchangeRoute = 'list' | 'runs' | 'sync' | 'fills';

const ROUTE_PATHS: Readonly<Record<ExchangeRoute, string>> = {
  list: EXCHANGES_PATH,
  runs: EXCHANGE_RUNS_PATH,
  sync: EXCHANGE_SYNC_PATH,
  fills: EXCHANGE_FILLS_PATH,
};

/** `2**63 - 1`, the offset's upper bound (spec 024, "Offset bound"). */
const MAX_OFFSET = 9_223_372_036_854_775_807n;

/**
 * An instant as `datetime.fromisoformat` reads it **with an offset**. A bound without one is
 * naive, and naive is a 422; so is a bare date or a digit string, which parse as naive.
 */
const AWARE_INSTANT =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})$/;

const FILL_QUERY_PARAMS: readonly string[] = ['exchange', 'from', 'to', 'limit', 'offset'];

export interface FakeExchangesOptions {
  readonly exchanges?: readonly ExchangeResponse[];
  readonly runs?: readonly ExchangeSyncRunResponse[];
  /**
   * The owner's stored fills, served by `GET /api/exchanges/fills`.
   *
   * **When given, the list is held to them**: every listed venue's `fills_stored` is the
   * number of rows of that venue, and every row belongs to a listed venue, because both are
   * `COUNT(*)`s of one table. When omitted, the endpoint answers an empty set and the list
   * is not checked against it. The page tests written before spec 024 never look at the
   * transactions, and an empty answer beside a non-zero count is what a sync committing
   * between the two reads produces.
   */
  readonly fills?: readonly ExchangeFill[];
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
  fills(): readonly ExchangeFill[];
  /**
   * Stores `added` as a sync would: the rows, and each venue's `fills_stored` moving by the
   * number of its rows, together.
   */
  addFills(added: readonly ExchangeFill[]): void;
  /** The query string of every `GET /api/exchanges/fills` that arrived, oldest first. */
  fillQueries(): URLSearchParams[];
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
  const modelsFills = options.fills !== undefined;
  let fills: readonly ExchangeFill[] = assertWritableFills([...(options.fills ?? [])]);
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

  /** Throws unless the list and the fills are counts of one table (see `options.fills`). */
  function assertFillsMatchList(): void {
    if (!modelsFills) {
      return;
    }
    for (const entry of exchanges) {
      const held = fills.filter((row) => row.exchange_key === entry.exchange_key).length;
      if (entry.fills_stored !== held) {
        throw new Error(
          `Impossible fake: ${entry.exchange_key} says fills_stored ${String(entry.fills_stored)} ` +
            `and the fake holds ${String(held)} of its fills. Both are COUNT(*)s of one table.`,
        );
      }
    }
    const listed = new Set(exchanges.map((entry) => entry.exchange_key));
    const orphan = fills.find((row) => !listed.has(row.exchange_key));
    if (orphan !== undefined) {
      throw new Error(
        `Impossible fake: fill ${String(orphan.id)} is on ${orphan.exchange_key}, which has ` +
          'no entry in the list. A fill is stored only under an account, and the list names ' +
          'every venue with an account.',
      );
    }
  }

  assertFillsMatchList();

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
      assertFillsMatchList();
    },
    patchExchange: (key, patch) => {
      exchanges = exchanges.map((entry) =>
        entry.exchange_key === key ? assertWritableExchange({ ...entry, ...patch }) : entry,
      );
      assertFillsMatchList();
    },
    setRuns: (next) => {
      runs = [...next];
    },
    fills: () => fills,
    addFills: (added) => {
      fills = assertWritableFills([...fills, ...added]);
      exchanges = exchanges.map((entry) => {
        const count = added.filter((row) => row.exchange_key === entry.exchange_key).length;
        return count === 0
          ? entry
          : assertWritableExchange({ ...entry, fills_stored: entry.fills_stored + count });
      });
      assertFillsMatchList();
    },
    fillQueries: () =>
      requests
        .filter((entry) => routeOf(entry) === 'fills')
        .map((entry) => new URL(entry.url).searchParams),
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

    http.get(EXCHANGE_FILLS_PATH, async ({ request }) => {
      const failure = await arrive('fills', request);
      if (failure !== undefined) {
        return failure;
      }
      const query = readFillQuery(new URL(request.url).searchParams);
      return typeof query === 'string'
        ? problem(422, 'Unprocessable Content', query)
        : HttpResponse.json(fillsPage(fills, query));
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

/** A query parameter that must be a base-10 integer: `null` when absent. */
function integerParam(params: URLSearchParams, name: string): bigint | null | 'invalid' {
  const raw = params.get(name);
  if (raw === null) {
    return null;
  }
  return /^-?\d+$/.test(raw) ? BigInt(raw) : 'invalid';
}

/** A bound, in milliseconds: `null` when absent. */
function instantParam(params: URLSearchParams, name: string): number | null | 'invalid' {
  const raw = params.get(name);
  if (raw === null) {
    return null;
  }
  return AWARE_INSTANT.test(raw) ? Date.parse(raw.replace(/(\.\d{3})\d+/, '$1')) : 'invalid';
}

/**
 * The endpoint's query rules (spec 024, "Endpoint"), or the reason for its 422.
 *
 * The page never means to send a refused query, so the fake refuses it as the backend
 * would, and the test that caused it sees an error state rather than rows served as if the
 * query had worked.
 */
function readFillQuery(params: URLSearchParams): FillQuery | string {
  const known: readonly string[] = ALL_EXCHANGE_KEYS;
  const venues = params.getAll('exchange');
  const unknown = venues.find((value) => !known.includes(value));
  if (unknown !== undefined) {
    return `exchange: "${unknown}" is not an ExchangeKey.`;
  }
  const from = instantParam(params, 'from');
  const to = instantParam(params, 'to');
  if (from === 'invalid' || to === 'invalid') {
    return 'from and to must be timezone-aware ISO 8601 datetimes.';
  }
  if (from !== null && to !== null && from >= to) {
    return 'from must be before to.';
  }
  const limit = integerParam(params, 'limit');
  if (limit === 'invalid' || (limit !== null && (limit < 1n || limit > BigInt(MAX_FILLS_LIMIT)))) {
    return `limit must be an integer from 1 to ${String(MAX_FILLS_LIMIT)}.`;
  }
  const offset = integerParam(params, 'offset');
  if (offset === 'invalid' || (offset !== null && (offset < 0n || offset > MAX_OFFSET))) {
    return 'offset must be an integer from 0.';
  }
  const unexpected = [...params.keys()].find((name) => !FILL_QUERY_PARAMS.includes(name));
  if (unexpected !== undefined) {
    // FastAPI ignores an unknown parameter. The page sending one is still a defect: the
    // filter it meant to apply is silently not applied.
    return `"${unexpected}" is not a parameter of this endpoint.`;
  }
  return {
    exchanges: [...new Set(venues)] as ExchangeKey[],
    from,
    to,
    limit: limit === null ? DEFAULT_FILLS_LIMIT : Number(limit),
    offset: offset === null ? 0 : Number(offset),
  };
}
