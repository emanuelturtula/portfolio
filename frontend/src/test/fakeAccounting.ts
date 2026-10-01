import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  assertWritablePositions,
  emptySnapshot,
  type PositionsResponse,
} from './accountingFixtures';
import type { RecordedRequest } from './fakePortfolio';
import {
  assertSameSnapshot,
  assertWritableReconciliation,
  matchingReconciliation,
  type ReconciliationResponse,
} from './reconciliationFixtures';
import { unauthorized } from './server';

export const POSITIONS_PATH = '/api/accounting/positions';
export const RECONCILIATION_PATH = '/api/accounting/reconciliation';

/** An accounting endpoint a test can hold open, or make fail. */
export type AccountingRoute = 'positions' | 'reconciliation';

const ROUTE_PATHS: Readonly<Record<AccountingRoute, string>> = {
  positions: POSITIONS_PATH,
  reconciliation: RECONCILIATION_PATH,
};

export interface FakeAccountingOptions {
  /**
   * What `GET /api/accounting/positions` answers. The default is {@link emptySnapshot}: the
   * first-time owner, whose startup recompute ran over no events.
   */
  readonly positions?: PositionsResponse;
  /**
   * What `GET /api/accounting/reconciliation` answers. The default is
   * {@link matchingReconciliation} over the positions: loaded, every held asset a match and
   * no source missing, so a test that is not about the holdings check gets no notice, no
   * table and no badge from it. A stated one must describe the same snapshot as the
   * positions - see {@link assertSameSnapshot}.
   */
  readonly reconciliation?: ReconciliationResponse;
  /**
   * The session this endpoint belongs to. While it is signed out, every request is answered
   * `401`, as the backend's deny-by-default middleware answers it.
   */
  readonly session?: { currentUser(): string | null };
}

export interface FakeAccounting {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  /** Every request either handler saw, oldest first, recorded on arrival. */
  readonly requests: RecordedRequest[];
  positions(): PositionsResponse;
  reconciliation(): ReconciliationResponse;
  /**
   * Replaces the snapshot, as a recompute on the server would, with no request from the page.
   * The reconciliation moves with it, because its history side is that snapshot: to
   * `reconciliation` when given, and otherwise to the quiet {@link matchingReconciliation}.
   */
  setPositions(next: PositionsResponse, reconciliation?: ReconciliationResponse): void;
  /** Replaces the comparison alone, as a balance read on the server would. Same snapshot. */
  setReconciliation(next: ReconciliationResponse): void;
  /**
   * Holds every request to `route` until the returned function is called. Recorded on
   * arrival. `route` defaults to the positions, the endpoint this fake had before spec 025.
   */
  hold(route?: AccountingRoute): () => void;
  /** Answers every request to `route` with `respond()` until called again with `null`. */
  fail(respond: (() => Response) | null, route?: AccountingRoute): void;
  /** How many requests reached `route`, including held and failed ones. */
  count(route?: AccountingRoute): number;
  /** The requests that reached `route`. */
  requestsTo(route: AccountingRoute): RecordedRequest[];
}

/**
 * A stateful fake of the two accounting endpoints: `GET /api/accounting/positions` and
 * `GET /api/accounting/reconciliation`.
 *
 * Every response it serves goes through {@link assertWritablePositions} or
 * {@link assertWritableReconciliation}, and the pair through {@link assertSameSnapshot}, so a
 * test cannot put on screen a snapshot the backend could not have served, nor a comparison
 * of some other snapshot than the one beside it.
 *
 * `hold`, `fail` and `count` act on one route, the positions unless stated: the two reads
 * are separate requests, and the page is required to survive one failing without the other.
 */
export function fakeAccounting(options: FakeAccountingOptions = {}): FakeAccounting {
  let positions = assertWritablePositions(options.positions ?? emptySnapshot());
  let reconciliation = checked(
    options.reconciliation ?? matchingReconciliation(positions),
    positions,
  );
  const holds = new Map<AccountingRoute, Promise<void>>();
  const failures = new Map<AccountingRoute, () => Response>();
  const requests: RecordedRequest[] = [];

  /** `next`, held to the backend's rules and to the snapshot it is served beside. */
  function checked(
    next: ReconciliationResponse,
    beside: PositionsResponse,
  ): ReconciliationResponse {
    assertSameSnapshot(assertWritableReconciliation(next), beside);
    return next;
  }

  function record(request: Request): void {
    requests.push({
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body: undefined,
    });
  }

  function requestsTo(route: AccountingRoute): RecordedRequest[] {
    return requests.filter((entry) => new URL(entry.url).pathname === ROUTE_PATHS[route]);
  }

  /** Records, waits out a hold, then answers a staged failure if there is one. */
  async function arrive(route: AccountingRoute, request: Request): Promise<Response | undefined> {
    record(request);
    await holds.get(route);
    return failures.get(route)?.();
  }

  const fake: FakeAccounting = {
    handlers: [],
    requests,
    positions: () => positions,
    reconciliation: () => reconciliation,
    setPositions: (next, nextReconciliation) => {
      // Both are checked before either is replaced: a refused pair leaves the fake as it was.
      const snapshot = assertWritablePositions(next);
      const comparison = checked(nextReconciliation ?? matchingReconciliation(snapshot), snapshot);
      positions = snapshot;
      reconciliation = comparison;
    },
    setReconciliation: (next) => {
      reconciliation = checked(next, positions);
    },
    hold: (route = 'positions') => {
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
    fail: (respond, route = 'positions') => {
      if (respond === null) {
        failures.delete(route);
      } else {
        failures.set(route, respond);
      }
    },
    count: (route = 'positions') => requestsTo(route).length,
    requestsTo,
  };

  fake.handlers.push(
    http.all('/api/accounting/*', ({ request }) => {
      if (options.session?.currentUser() !== null) {
        return undefined;
      }
      record(request);
      return unauthorized();
    }),
    http.get(POSITIONS_PATH, async ({ request }) => {
      const failure = await arrive('positions', request);
      return failure ?? HttpResponse.json(positions);
    }),
    http.get(RECONCILIATION_PATH, async ({ request }) => {
      const failure = await arrive('reconciliation', request);
      return failure ?? HttpResponse.json(reconciliation);
    }),
  );

  return fake;
}
