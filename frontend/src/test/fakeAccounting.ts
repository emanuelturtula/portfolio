import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  assertWritablePositions,
  emptySnapshot,
  type PositionsResponse,
} from './accountingFixtures';
import type { RecordedRequest } from './fakePortfolio';
import { unauthorized } from './server';

export const POSITIONS_PATH = '/api/accounting/positions';

export interface FakeAccountingOptions {
  /**
   * What `GET /api/accounting/positions` answers. The default is {@link emptySnapshot}: the
   * first-time owner, whose startup recompute ran over no events.
   */
  readonly positions?: PositionsResponse;
  /**
   * The session this endpoint belongs to. While it is signed out, every request is answered
   * `401`, as the backend's deny-by-default middleware answers it.
   */
  readonly session?: { currentUser(): string | null };
}

export interface FakeAccounting {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  /** Every request the handler saw, oldest first, recorded on arrival. */
  readonly requests: RecordedRequest[];
  positions(): PositionsResponse;
  /** Replaces the snapshot, as a recompute on the server would, with no request from the page. */
  setPositions(next: PositionsResponse): void;
  /** Holds every request until the returned function is called. Recorded on arrival. */
  hold(): () => void;
  /** Answers every request with `respond()` until called again with `null`. Still recorded. */
  fail(respond: (() => Response) | null): void;
  /** How many requests reached the endpoint, including held and failed ones. */
  count(): number;
}

/**
 * A stateful fake of `GET /api/accounting/positions`, the one accounting endpoint.
 *
 * Every response it serves goes through {@link assertWritablePositions}, so a test cannot put
 * a snapshot on screen that the backend could not have served.
 */
export function fakeAccounting(options: FakeAccountingOptions = {}): FakeAccounting {
  let positions = assertWritablePositions(options.positions ?? emptySnapshot());
  let held: Promise<void> | undefined;
  let failure: (() => Response) | undefined;
  const requests: RecordedRequest[] = [];

  function record(request: Request): void {
    requests.push({
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body: undefined,
    });
  }

  const fake: FakeAccounting = {
    handlers: [],
    requests,
    positions: () => positions,
    setPositions: (next) => {
      positions = assertWritablePositions(next);
    },
    hold: () => {
      let release: () => void = () => undefined;
      held = new Promise<void>((resolve) => {
        release = resolve;
      });
      return () => {
        held = undefined;
        release();
      };
    },
    fail: (respond) => {
      failure = respond ?? undefined;
    },
    count: () => requests.length,
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
      record(request);
      await held;
      return failure?.() ?? HttpResponse.json(positions);
    }),
  );

  return fake;
}
