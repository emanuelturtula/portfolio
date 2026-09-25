import { onTestFinished } from 'vitest';
import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  currentBalances,
  NOW,
  syncRun,
  triggered,
  unreadBalance,
  unreadEntry,
  type CurrentBalancesResponse,
  type SyncRunResponse,
  type SyncTriggeredResponse,
  type WalletResponse,
} from './fixtures';
import { problem, refuseNonJsonWrite, server, unauthorized } from './server';

export const WALLETS_PATH = '/api/wallets';
export const WALLET_PATH = '/api/wallets/:walletId';
export const BALANCES_CURRENT_PATH = '/api/balances/current';
export const BALANCES_RUNS_PATH = '/api/balances/runs';
export const BALANCES_SYNC_PATH = '/api/balances/sync';

/**
 * The exact sentences the backend answers with, copied from
 * `backend/src/portfolio/services/wallets.py` and
 * `backend/src/portfolio/domain/addresses.py`. Written out literally so that a
 * wording change on either side shows up as a diff here.
 */
export const DUPLICATE_DETAIL = 'This address is already registered for this chain.';
export const DUPLICATE_ARCHIVED_DETAIL =
  'This address is already registered for this chain and is archived. ' +
  'Restore it instead of adding it again.';
export const WALLET_NOT_FOUND_DETAIL = 'No wallet with that id.';
export const VALIDATION_DETAIL = 'The request parameters failed validation.';

/** A subset of `REJECTION_MESSAGES`, keyed by the `type` the router sends. */
export const ADDRESS_REJECTIONS = {
  bad_checksum: 'The checksum does not match.',
  extended_key: 'This is an extended public key, not an address.',
  wrong_network: 'The address belongs to a different network of this chain.',
  malformed: 'The address is not shaped like an address for this chain.',
} as const;

export type AddressRejectionType = keyof typeof ADDRESS_REJECTIONS;

/** What Pydantic says about a label over `MAX_LABEL_LENGTH` (100). */
export const LABEL_TOO_LONG = {
  msg: 'String should have at most 100 characters',
  type: 'string_too_long',
} as const;
export const MAX_LABEL_LENGTH = 100;

export interface FieldErrorEntry {
  readonly loc: readonly (string | number)[];
  readonly msg: string;
  readonly type: string;
}

/**
 * The 422 the backend renders through `api/errors.py`: a problem document plus
 * `errors: [{loc, msg, type}]`, with `loc` as strings.
 */
export function validationProblem(errors: readonly FieldErrorEntry[]): Response {
  return HttpResponse.json(
    {
      type: 'about:blank',
      title: 'Unprocessable Entity',
      status: 422,
      detail: VALIDATION_DETAIL,
      instance: WALLETS_PATH,
      errors,
    },
    { status: 422, headers: { 'content-type': 'application/problem+json' } },
  );
}

/** One request the fake saw, in order. */
export interface RecordedRequest {
  readonly method: string;
  readonly url: string;
  readonly contentType: string | null;
  readonly body: unknown;
}

/** A write route a test can hold open, to observe the page while it is in flight. */
export type HoldableRoute = 'create' | 'archive' | 'restore' | 'sync';

/**
 * A current view computed from the registry at request time, for flows where
 * archiving or restoring a wallet has to change what the next balance read
 * returns - as it does on the backend, which excludes archived wallets.
 */
export type CurrentView = (activeWallets: readonly WalletResponse[]) => CurrentBalancesResponse;

export interface FakePortfolioOptions {
  readonly wallets?: readonly WalletResponse[];
  /**
   * The current view. When omitted, it is derived from the active wallets the
   * way the backend derives it for wallets no run has read: every one unread,
   * with nulls, a total of `"0"` and `complete` false unless there are none.
   */
  readonly current?: CurrentBalancesResponse | CurrentView;
  readonly runs?: readonly SyncRunResponse[];
  /**
   * What `POST /api/balances/sync` does. Receives the fake so it can replace
   * the current view and the run log the way a real run would. The default
   * answers with the newest run as a manual, unjoined summary and changes
   * nothing.
   */
  readonly onSync?: (fake: FakePortfolio) => SyncTriggeredResponse;
  /**
   * The session these endpoints belong to. When given, every request made
   * while it is signed out is answered `401`, as the backend's deny-by-default
   * middleware answers it. Without this, a query rebuilt in the instant
   * between a `401` and the redirect is answered with data the dead session
   * could never have read, and a test can pass on a cache the real backend
   * would have refused to fill.
   */
  readonly session?: { currentUser(): string | null };
}

export interface FakePortfolio {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  /** Every request any of these handlers saw, oldest first. */
  readonly requests: RecordedRequest[];
  wallets(): readonly WalletResponse[];
  current(): CurrentBalancesResponse;
  runs(): readonly SyncRunResponse[];
  setCurrent(current: CurrentBalancesResponse | CurrentView | undefined): void;
  setRuns(runs: readonly SyncRunResponse[]): void;
  /** The next create of this address answers a 422 on `["body", "address"]`. */
  rejectAddress(address: string, rejection: AddressRejectionType): void;
  /**
   * Holds every request to `route` until the returned function is called.
   * The request is recorded when it arrives, not when it is released.
   */
  hold(route: HoldableRoute): () => void;
  /** Requests of one method to one path, e.g. `writes('POST', WALLETS_PATH)`. */
  writes(method: string, path: string): RecordedRequest[];
}

/**
 * A stateful fake of the wallet registry and the balance endpoints.
 *
 * Writes go through {@link refuseNonJsonWrite}, the same guard the backend
 * applies, so a client that stops sending `Content-Type: application/json` on
 * a bodyless `DELETE` fails here rather than in production.
 *
 * Add, archive and restore change the state the next `GET` reads, which is
 * what lets a test assert on the list the page re-reads after a mutation
 * rather than on a cache it patched by hand.
 */
export function fakePortfolio(options: FakePortfolioOptions = {}): FakePortfolio {
  let wallets: WalletResponse[] = (options.wallets ?? []).map((row) => ({ ...row }));
  let current: CurrentBalancesResponse | CurrentView | undefined = options.current;
  let runs: SyncRunResponse[] = [...(options.runs ?? [])];
  const rejections = new Map<string, AddressRejectionType>();
  const holds = new Map<HoldableRoute, Promise<void>>();
  const requests: RecordedRequest[] = [];

  async function record(request: Request): Promise<unknown> {
    const body = await readJsonBody(request);
    requests.push({
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body,
    });
    return body;
  }

  async function waitFor(route: HoldableRoute): Promise<void> {
    await holds.get(route);
  }

  function nextId(): number {
    return wallets.reduce((max, row) => Math.max(max, row.id), 0) + 1;
  }

  function findWallet(rawId: unknown): WalletResponse | undefined {
    return wallets.find((row) => String(row.id) === String(rawId));
  }

  function derivedCurrent(): CurrentBalancesResponse {
    const active = wallets.filter((row) => !row.archived);

    return currentBalances({
      complete: active.length === 0,
      wallets: active.map((row) => unreadBalance(row)),
      unread: active.map((row) => unreadEntry(row)),
    });
  }

  const fake: FakePortfolio = {
    handlers: [],
    requests,
    wallets: () => wallets,
    current: () => {
      if (current === undefined) {
        return derivedCurrent();
      }
      return typeof current === 'function'
        ? current(wallets.filter((row) => !row.archived))
        : current;
    },
    runs: () => runs,
    setCurrent: (next) => {
      current = next;
    },
    setRuns: (next) => {
      runs = [...next];
    },
    rejectAddress: (address, rejection) => {
      rejections.set(address, rejection);
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
    writes: (method, path) =>
      requests.filter((entry) => entry.method === method && new URL(entry.url).pathname === path),
  };

  /** Answers `401` while the session is signed out; otherwise falls through. */
  const requireSession = async ({ request }: { request: Request }) => {
    // No session given, or one that is signed in: let the route answer.
    if (options.session?.currentUser() !== null) {
      return undefined;
    }
    await record(request);
    return unauthorized();
  };

  const handlers: HttpHandler[] = [
    http.all(WALLETS_PATH, requireSession),
    http.all(WALLET_PATH, requireSession),
    http.all('/api/balances/*', requireSession),
    http.get(WALLETS_PATH, async ({ request }) => {
      await record(request);
      const includeArchived = new URL(request.url).searchParams.get('include_archived') === 'true';

      return HttpResponse.json({
        wallets: wallets.filter((row) => includeArchived || !row.archived),
      });
    }),

    http.post(WALLETS_PATH, async ({ request }) => {
      const body = (await record(request)) as
        { chain_key?: unknown; address?: unknown; label?: unknown } | undefined;
      await waitFor('create');

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      const chainKey = body?.chain_key;
      const address = typeof body?.address === 'string' ? body.address.trim() : '';
      const label = typeof body?.label === 'string' ? body.label.trim() : null;

      if (chainKey !== 'bitcoin' && chainKey !== 'kaspa') {
        return validationProblem([
          {
            loc: ['body', 'chain_key'],
            msg: "Input should be 'bitcoin' or 'kaspa'",
            type: 'enum',
          },
        ]);
      }

      if (label !== null && label.length > MAX_LABEL_LENGTH) {
        return validationProblem([{ loc: ['body', 'label'], ...LABEL_TOO_LONG }]);
      }

      const rejection = rejections.get(address);
      if (rejection !== undefined) {
        rejections.delete(address);
        return validationProblem([
          { loc: ['body', 'address'], msg: ADDRESS_REJECTIONS[rejection], type: rejection },
        ]);
      }

      if (address === '') {
        return validationProblem([
          {
            loc: ['body', 'address'],
            msg: 'String should have at least 1 character',
            type: 'string_too_short',
          },
        ]);
      }

      const existing = wallets.find((row) => row.chain_key === chainKey && row.address === address);
      if (existing !== undefined) {
        return problem(
          409,
          'Conflict',
          existing.archived ? DUPLICATE_ARCHIVED_DETAIL : DUPLICATE_DETAIL,
        );
      }

      const created: WalletResponse = {
        id: nextId(),
        chain_key: chainKey,
        address,
        label: label === '' ? null : label,
        archived: false,
        created_at: NOW,
        updated_at: NOW,
      };
      wallets = [...wallets, created];

      return HttpResponse.json(created, { status: 201 });
    }),

    http.patch(WALLET_PATH, async ({ request, params }) => {
      const body = (await record(request)) as { archived?: unknown; label?: unknown } | undefined;
      await waitFor(body?.archived === false ? 'restore' : 'archive');

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      const target = findWallet(params.walletId);
      if (target === undefined) {
        return problem(404, 'Not Found', WALLET_NOT_FOUND_DETAIL);
      }

      const updated: WalletResponse = {
        ...target,
        archived: typeof body?.archived === 'boolean' ? body.archived : target.archived,
        label: body !== undefined && 'label' in body ? (body.label as string | null) : target.label,
        updated_at: NOW,
      };
      wallets = wallets.map((row) => (row.id === target.id ? updated : row));

      return HttpResponse.json(updated);
    }),

    http.delete(WALLET_PATH, async ({ request, params }) => {
      await record(request);
      await waitFor('archive');

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      const target = findWallet(params.walletId);
      if (target === undefined) {
        return problem(404, 'Not Found', WALLET_NOT_FOUND_DETAIL);
      }

      wallets = wallets.map((row) =>
        row.id === target.id ? { ...row, archived: true, updated_at: NOW } : row,
      );

      return new HttpResponse(null, { status: 204 });
    }),

    http.get(BALANCES_CURRENT_PATH, async ({ request }) => {
      await record(request);
      return HttpResponse.json(fake.current());
    }),

    http.get(BALANCES_RUNS_PATH, async ({ request }) => {
      await record(request);
      const rawLimit = new URL(request.url).searchParams.get('limit');
      const limit = rawLimit === null ? 20 : Number.parseInt(rawLimit, 10);

      return HttpResponse.json({ runs: runs.slice(0, limit) });
    }),

    http.post(BALANCES_SYNC_PATH, async ({ request }) => {
      await record(request);
      await waitFor('sync');

      const refusal = refuseNonJsonWrite(request);
      if (refusal !== undefined) {
        return refusal;
      }

      if (options.onSync !== undefined) {
        return HttpResponse.json(options.onSync(fake));
      }

      return HttpResponse.json(triggered(runs[0] ?? syncRun({ trigger: 'manual' })));
    }),
  ];

  fake.handlers.push(...handlers);

  return fake;
}

/**
 * Records the URL of every request the application makes, through any
 * handler, for the rest of the current test.
 *
 * Listens on the server rather than inside {@link fakePortfolio}'s handlers,
 * so a request answered by a per-test override - a 422, a 500 - is recorded
 * too. The privacy rule is about what leaves the page, not about which
 * handler happened to answer it.
 */
export function recordRequestUrls(): string[] {
  const urls: string[] = [];
  const listener = ({ request }: { request: Request }): void => {
    urls.push(request.url);
  };

  server.events.on('request:start', listener);
  onTestFinished(() => {
    server.events.removeListener('request:start', listener);
  });

  return urls;
}

/** Reads a request body as JSON, tolerating the bodyless writes this API has. */
async function readJsonBody(request: Request): Promise<unknown> {
  try {
    return (await request.clone().json()) as unknown;
  } catch {
    return undefined;
  }
}
