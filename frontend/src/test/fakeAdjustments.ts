import { http, HttpResponse, type HttpHandler } from 'msw';

import {
  adjustmentList,
  asStoredAmount,
  asStoredInstant,
  ASSET_SYMBOL_PATTERN,
  ASSET_SYMBOL_RULE,
  assertWritableAdjustment,
  assertWritableAdjustments,
  assertWritableFirstTrades,
  ADJUSTMENT_NOT_FOUND_DETAIL,
  AMOUNT_SCALE,
  CASH_ASSET_RULE,
  CASH_ASSETS,
  Exact,
  firstTrades as noFirstTrades,
  inReplayOrder,
  instantKey,
  MAX_AMOUNT_INTEGER_DIGITS,
  NOTE_BLANK_RULE,
  NOTE_MAX_LENGTH,
  NOTE_TOO_LONG_RULE,
  OCCURRED_IN_FUTURE_RULE,
  OCCURRED_NAIVE_RULE,
  QUANTITY_NOT_POSITIVE_RULE,
  QUANTITY_TOO_LARGE_RULE,
  QUANTITY_TOO_PRECISE_RULE,
  REFUSAL_TYPE,
  TOTAL_COST_TOO_LARGE_RULE,
  UNIT_COST_NEGATIVE_RULE,
  UNIT_COST_TOO_LARGE_RULE,
  UNIT_COST_TOO_PRECISE_RULE,
  VALIDATION_DETAIL,
  type AdjustmentResponse,
  type FirstTradesResponse,
} from './adjustmentFixtures';
import type { FieldErrorEntry } from './fakePortfolio';
import { problem, refuseNonJsonWrite, unauthorized } from './server';

export const ADJUSTMENTS_PATH = '/api/accounting/adjustments';
export const ADJUSTMENT_PATH = '/api/accounting/adjustments/:adjustmentId';
export const FIRST_TRADES_PATH = '/api/accounting/first-trades';

/** The path of one adjustment, as a request names it. */
export function adjustmentPath(id: number): string {
  return `${ADJUSTMENTS_PATH}/${String(id)}`;
}

/** An endpoint a test can hold open, or make fail. */
export type AdjustmentRoute = 'list' | 'create' | 'replace' | 'delete' | 'first-trades';

/** One request the fake saw, in order, with the body as it was sent and as it parses. */
export interface RecordedAdjustmentRequest {
  readonly method: string;
  readonly url: string;
  readonly contentType: string | null;
  /** The body parsed as JSON, or `undefined` when there was none. */
  readonly body: unknown;
  /** The body's own bytes as text: `""` when the request carried none. */
  readonly text: string;
}

/**
 * The 422 the backend renders through `api/errors.py` for a refused adjustment: a problem
 * document plus `errors: [{loc, msg, type}]`, with `instance` the path that was asked for.
 */
export function adjustmentValidationProblem(
  errors: readonly FieldErrorEntry[],
  instance: string = ADJUSTMENTS_PATH,
): Response {
  return HttpResponse.json(
    {
      type: 'about:blank',
      title: 'Unprocessable Entity',
      status: 422,
      detail: VALIDATION_DETAIL,
      instance,
      errors,
    },
    { status: 422, headers: { 'content-type': 'application/problem+json' } },
  );
}

/** One entry of a refusal by the service: `["body", field]`, the rule, and `value_error`. */
export function refusal(field: string, rule: string): FieldErrorEntry {
  return { loc: ['body', field], msg: rule, type: REFUSAL_TYPE };
}

/** The 404 of a missing adjustment, and of another owner's: it says neither. */
export function adjustmentNotFound(): Response {
  return problem(404, 'Not Found', ADJUSTMENT_NOT_FOUND_DETAIL);
}

export interface FakeAdjustmentsOptions {
  /** The owner's adjustments. Any order: the fake serves them in the endpoint's. */
  readonly adjustments?: readonly AdjustmentResponse[];
  /** What `GET /api/accounting/first-trades` answers. The default is an owner with no fills. */
  readonly firstTrades?: FirstTradesResponse;
  /**
   * The session these endpoints belong to. While it is signed out, every request is answered
   * `401`, as the backend's deny-by-default middleware answers it.
   */
  readonly session?: { currentUser(): string | null };
  /**
   * Runs after a create, a replace or a delete has changed the state and **before** it is
   * answered: where the backend recomputes the positions (spec 023). A test that needs the
   * recompute to be visible replaces the snapshot `fakeAccounting` serves here.
   */
  readonly onChange?: () => void;
}

export interface FakeAdjustments {
  /** Register these with `server.use(...fake.handlers)`. */
  readonly handlers: HttpHandler[];
  /** Every request any of these handlers saw, oldest first, recorded on arrival. */
  readonly requests: RecordedAdjustmentRequest[];
  /** The stored adjustments, in the endpoint's order. */
  adjustments(): readonly AdjustmentResponse[];
  firstTrades(): FirstTradesResponse;
  setFirstTrades(next: FirstTradesResponse): void;
  /**
   * Changes the stored adjustments as another session would: on the server, with no request
   * from this page, so what the page shows goes stale until it next reads.
   */
  replaceElsewhere(next: readonly AdjustmentResponse[]): void;
  /** Deletes one as another session would. */
  deleteElsewhere(id: number): void;
  /**
   * Holds every request to `route` until the returned function is called. The request is
   * recorded when it arrives, not when it is released.
   */
  hold(route: AdjustmentRoute): () => void;
  /** Answers every request to `route` with `respond()` until called again with `null`. */
  fail(route: AdjustmentRoute, respond: (() => Response) | null): void;
  /** How many requests reached `route`, including held and failed ones. */
  count(route: AdjustmentRoute): number;
  /** The requests that reached `route`. */
  requestsTo(route: AdjustmentRoute): RecordedAdjustmentRequest[];
}

const BODY_FIELDS = ['asset', 'quantity', 'unit_cost', 'occurred_at', 'note'] as const;
type BodyField = (typeof BODY_FIELDS)[number];

const PLAIN_DECIMAL = /^[+-]?(?:\d+\.?\d*|\.\d+)$/;

/** A draft whose shape Pydantic accepted: what `validate_draft` is then handed. */
interface Draft {
  readonly asset: string;
  readonly quantity: string;
  readonly unit_cost: string | null;
  /** The instant in its stored form, or `'naive'` for a datetime with no offset. */
  readonly occurred_at: string;
  readonly note: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/**
 * The request models' own checks (`api/schemas/adjustments.py`): shape, never the rules.
 * Every failure is reported at once, as Pydantic reports them, in field order.
 *
 * An amount is a JSON string and only one: a JSON number is refused whatever its value, so a
 * page that sent `1.5` rather than `"1.5"` fails here, in the test, and not in production.
 */
function parseBody(
  body: unknown,
  unitCostRequired: boolean,
): { draft: Draft } | { errors: FieldErrorEntry[] } {
  if (!isRecord(body)) {
    return {
      errors: [{ loc: ['body'], msg: 'Input should be a valid dictionary', type: 'dict_type' }],
    };
  }

  const errors: FieldErrorEntry[] = [];
  const missing = (field: BodyField): void => {
    errors.push({ loc: ['body', field], msg: 'Field required', type: 'missing' });
  };

  let asset = '';
  if (!('asset' in body)) {
    missing('asset');
  } else if (typeof body.asset !== 'string') {
    errors.push({
      loc: ['body', 'asset'],
      msg: 'Input should be a valid string',
      type: 'string_type',
    });
  } else {
    asset = body.asset;
  }

  const amount = (field: 'quantity' | 'unit_cost', value: unknown): string => {
    if (typeof value === 'number' || typeof value === 'boolean') {
      errors.push({
        loc: ['body', field],
        msg: 'Value error, a monetary value must arrive as a JSON string',
        type: 'value_error',
      });
      return '';
    }
    if (typeof value !== 'string') {
      errors.push({
        loc: ['body', field],
        msg: 'Decimal input should be an integer, float, string or Decimal object',
        type: 'decimal_type',
      });
      return '';
    }
    if (/[eE]/.test(value.trim()) && !/[^0-9eE+.-]/.test(value.trim())) {
      // Python's `Decimal` reads exponent notation, and what the engine then makes of it is
      // more than this fake models. Refusing loudly beats answering something the backend
      // would not.
      throw new Error(`fakeAdjustments does not model an amount in exponent notation: ${field}.`);
    }
    if (!PLAIN_DECIMAL.test(value.trim())) {
      errors.push({
        loc: ['body', field],
        msg: 'Input should be a valid decimal',
        type: 'decimal_parsing',
      });
      return '';
    }
    return value.trim();
  };

  let quantity = '';
  if (!('quantity' in body)) {
    missing('quantity');
  } else {
    quantity = amount('quantity', body.quantity);
  }

  let unitCost: string | null = null;
  if (!('unit_cost' in body)) {
    if (unitCostRequired) {
      missing('unit_cost');
    }
  } else if (body.unit_cost !== null) {
    unitCost = amount('unit_cost', body.unit_cost);
  }

  let occurredAt = '';
  if (!('occurred_at' in body)) {
    missing('occurred_at');
  } else if (typeof body.occurred_at === 'number' || typeof body.occurred_at === 'boolean') {
    errors.push({
      loc: ['body', 'occurred_at'],
      msg:
        'Value error, occurred_at must arrive as an ISO 8601 string with a timezone, not as ' +
        'a JSON number',
      type: 'value_error',
    });
  } else if (typeof body.occurred_at !== 'string') {
    errors.push({
      loc: ['body', 'occurred_at'],
      msg: 'Input should be a valid datetime',
      type: 'datetime_type',
    });
  } else {
    const stored = asStoredInstant(body.occurred_at);
    if (stored === null) {
      errors.push({
        loc: ['body', 'occurred_at'],
        msg:
          'Value error, occurred_at must be an ISO 8601 datetime with a timezone, such as ' +
          '2026-01-01T00:00:00Z',
        type: 'value_error',
      });
    } else {
      occurredAt = stored;
    }
  }

  let note = '';
  if (!('note' in body)) {
    missing('note');
  } else if (typeof body.note !== 'string') {
    errors.push({
      loc: ['body', 'note'],
      msg: 'Input should be a valid string',
      type: 'string_type',
    });
  } else {
    note = body.note;
  }

  for (const key of Object.keys(body)) {
    if (!(BODY_FIELDS as readonly string[]).includes(key)) {
      errors.push({
        loc: ['body', key],
        msg: 'Extra inputs are not permitted',
        type: 'extra_forbidden',
      });
    }
  }

  if (errors.length > 0) {
    return { errors };
  }
  return { draft: { asset, quantity, unit_cost: unitCost, occurred_at: occurredAt, note } };
}

const LIMIT = new Exact(10).pow(MAX_AMOUNT_INTEGER_DIGITS);

/** `_require_amount`, in its order: the sign, then the integer digits, then the places. */
function amountRule(
  value: string,
  rules: { readonly sign: string | null; readonly large: string; readonly precise: string },
  positive: boolean,
): string | null {
  const parsed = new Exact(value);
  if (positive ? parsed.lte(0) : parsed.lt(0)) {
    return rules.sign;
  }
  if (parsed.abs().gte(LIMIT)) {
    return rules.large;
  }
  if (parsed.decimalPlaces() > AMOUNT_SCALE) {
    return rules.precise;
  }
  return null;
}

/**
 * `validate_draft`, in its order - asset, occurred_at, quantity, unit_cost, note - and the
 * first failure is the one reported. `now` is the fake's clock, read when the request lands.
 */
function refusalOf(draft: Draft, now: string): FieldErrorEntry | null {
  if (!ASSET_SYMBOL_PATTERN.test(draft.asset)) {
    return refusal('asset', ASSET_SYMBOL_RULE);
  }
  if (CASH_ASSETS.includes(draft.asset)) {
    return refusal('asset', CASH_ASSET_RULE);
  }
  if (draft.occurred_at === 'naive') {
    return refusal('occurred_at', OCCURRED_NAIVE_RULE);
  }
  if (instantKey('occurred_at', draft.occurred_at) > instantKey('now', now)) {
    return refusal('occurred_at', OCCURRED_IN_FUTURE_RULE);
  }
  const quantityRule = amountRule(
    draft.quantity,
    {
      sign: QUANTITY_NOT_POSITIVE_RULE,
      large: QUANTITY_TOO_LARGE_RULE,
      precise: QUANTITY_TOO_PRECISE_RULE,
    },
    true,
  );
  if (quantityRule !== null) {
    return refusal('quantity', quantityRule);
  }
  if (draft.unit_cost !== null) {
    const costRule = amountRule(
      draft.unit_cost,
      {
        sign: UNIT_COST_NEGATIVE_RULE,
        large: UNIT_COST_TOO_LARGE_RULE,
        precise: UNIT_COST_TOO_PRECISE_RULE,
      },
      false,
    );
    if (costRule !== null) {
      return refusal('unit_cost', costRule);
    }
    if (new Exact(draft.unit_cost).times(new Exact(draft.quantity)).gte(LIMIT)) {
      return refusal('unit_cost', TOTAL_COST_TOO_LARGE_RULE);
    }
  }
  // Python's `str.strip()` and `len()`: whitespace-only is blank, and the length is counted
  // in code points on the note as given.
  if (draft.note.trim() === '') {
    return refusal('note', NOTE_BLANK_RULE);
  }
  if (Array.from(draft.note).length > NOTE_MAX_LENGTH) {
    return refusal('note', NOTE_TOO_LONG_RULE);
  }
  return null;
}

/**
 * A stateful fake of the four adjustment endpoints and of `GET /api/accounting/first-trades`.
 *
 * - **Writes go through {@link refuseNonJsonWrite}**, the guard the backend applies, so a
 *   client that stops sending `Content-Type: application/json` on the bodyless `DELETE` fails
 *   here rather than in production - which is the failure Swagger UI has with that endpoint.
 * - **A body is held to the request models and then to the service's rules**, in the
 *   backend's order and with its sentences, so a refusal on screen is one the backend sends.
 *   The body is validated before the id is looked up, as `AdjustmentService.update` does.
 * - **What is stored is what `view_of` serves**: amounts at eighteen places, the instant in
 *   UTC, the note exactly as given. Every stored row goes through
 *   {@link assertWritableAdjustment}.
 * - **Create, replace and delete change the state the next `GET` reads**, which is what lets
 *   a test assert on the list the page re-reads after a mutation, not on a cache it patched.
 *
 * It serves only its own three paths. The page also reads `GET /api/accounting/positions`,
 * which `fakeAccounting` serves: register both.
 */
export function fakeAdjustments(options: FakeAdjustmentsOptions = {}): FakeAdjustments {
  let stored: AdjustmentResponse[] = inReplayOrder(
    (options.adjustments ?? []).map((entry) => assertWritableAdjustment({ ...entry })),
  );
  assertWritableAdjustments(stored);
  let firstTrades = assertWritableFirstTrades(options.firstTrades ?? noFirstTrades());
  const holds = new Map<AdjustmentRoute, Promise<void>>();
  const failures = new Map<AdjustmentRoute, () => Response>();
  const requests: RecordedAdjustmentRequest[] = [];
  const routes = new WeakMap<RecordedAdjustmentRequest, AdjustmentRoute>();

  /** The fake's clock, as the backend would serialise a reading of it. */
  function now(): string {
    const stamp = asStoredInstant(new Date(Date.now()).toISOString());
    if (stamp === null || stamp === 'naive') {
      throw new Error('The clock gave no instant.');
    }
    return stamp;
  }

  // `AUTOINCREMENT`: an id is never reused, so the next one is above every one ever given.
  let highestId = 0;

  function nextId(): number {
    highestId = Math.max(highestId, ...stored.map((entry) => entry.id)) + 1;
    return highestId;
  }

  /**
   * Records the request, waits out a hold, then gives what refuses it before the route runs: a
   * staged failure if there is one, and otherwise the write guard's 403.
   */
  async function arrive(
    route: AdjustmentRoute,
    request: Request,
  ): Promise<{ entry: RecordedAdjustmentRequest; failure: Response | undefined }> {
    const text = await request.clone().text();
    let body: unknown;
    try {
      body = text === '' ? undefined : (JSON.parse(text) as unknown);
    } catch {
      body = undefined;
    }
    const entry: RecordedAdjustmentRequest = {
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body,
      text,
    };
    requests.push(entry);
    routes.set(entry, route);
    await holds.get(route);
    return { entry, failure: failures.get(route)?.() ?? refuseNonJsonWrite(request) };
  }

  function requestsTo(route: AdjustmentRoute): RecordedAdjustmentRequest[] {
    return requests.filter((entry) => routes.get(entry) === route);
  }

  function find(rawId: unknown): AdjustmentResponse | undefined {
    return stored.find((entry) => String(entry.id) === String(rawId));
  }

  /** The draft as the row `view_of` serves, with the id and the timestamps it is given. */
  function rowOf(
    draft: Draft,
    identity: Pick<AdjustmentResponse, 'id' | 'created_at' | 'updated_at'>,
  ): AdjustmentResponse {
    return assertWritableAdjustment({
      ...identity,
      asset: draft.asset,
      quantity: asStoredAmount(draft.quantity),
      unit_cost: draft.unit_cost === null ? null : asStoredAmount(draft.unit_cost),
      occurred_at: draft.occurred_at,
      note: draft.note,
    });
  }

  const fake: FakeAdjustments = {
    handlers: [],
    requests,
    adjustments: () => stored,
    firstTrades: () => firstTrades,
    setFirstTrades: (next) => {
      firstTrades = assertWritableFirstTrades(next);
    },
    replaceElsewhere: (next) => {
      const ordered = inReplayOrder(next.map((entry) => assertWritableAdjustment({ ...entry })));
      assertWritableAdjustments(ordered);
      stored = ordered;
    },
    deleteElsewhere: (id) => {
      stored = stored.filter((entry) => entry.id !== id);
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
    count: (route) => requestsTo(route).length,
    requestsTo,
  };

  /** Answers `401` while the session is signed out; otherwise falls through. */
  const requireSession = ({ request }: { request: Request }) => {
    if (options.session?.currentUser() !== null) {
      return undefined;
    }
    requests.push({
      method: request.method.toUpperCase(),
      url: request.url,
      contentType: request.headers.get('content-type'),
      body: undefined,
      text: '',
    });
    return unauthorized();
  };

  fake.handlers.push(
    http.all(ADJUSTMENTS_PATH, requireSession),
    http.all(ADJUSTMENT_PATH, requireSession),
    http.all(FIRST_TRADES_PATH, requireSession),

    http.get(ADJUSTMENTS_PATH, async ({ request }) => {
      const { failure } = await arrive('list', request);
      return failure ?? HttpResponse.json(adjustmentList(stored));
    }),

    http.post(ADJUSTMENTS_PATH, async ({ request }) => {
      const { entry, failure } = await arrive('create', request);
      if (failure !== undefined) {
        return failure;
      }
      const parsed = parseBody(entry.body, false);
      if ('errors' in parsed) {
        return adjustmentValidationProblem(parsed.errors);
      }
      const stamp = now();
      const refused = refusalOf(parsed.draft, stamp);
      if (refused !== null) {
        return adjustmentValidationProblem([refused]);
      }
      const created = rowOf(parsed.draft, { id: nextId(), created_at: stamp, updated_at: stamp });
      stored = inReplayOrder([...stored, created]);
      options.onChange?.();
      return HttpResponse.json(created, { status: 201 });
    }),

    http.put(ADJUSTMENT_PATH, async ({ request, params }) => {
      const { entry, failure } = await arrive('replace', request);
      if (failure !== undefined) {
        return failure;
      }
      const path = new URL(request.url).pathname;
      const parsed = parseBody(entry.body, true);
      if ('errors' in parsed) {
        return adjustmentValidationProblem(parsed.errors, path);
      }
      const stamp = now();
      const refused = refusalOf(parsed.draft, stamp);
      if (refused !== null) {
        return adjustmentValidationProblem([refused], path);
      }
      const target = find(params.adjustmentId);
      if (target === undefined) {
        return adjustmentNotFound();
      }
      const updated = rowOf(parsed.draft, {
        id: target.id,
        created_at: target.created_at,
        updated_at: stamp,
      });
      stored = inReplayOrder(stored.map((entry) => (entry.id === target.id ? updated : entry)));
      options.onChange?.();
      return HttpResponse.json(updated);
    }),

    http.delete(ADJUSTMENT_PATH, async ({ request, params }) => {
      const { failure } = await arrive('delete', request);
      if (failure !== undefined) {
        return failure;
      }
      const target = find(params.adjustmentId);
      if (target === undefined) {
        return adjustmentNotFound();
      }
      highestId = Math.max(highestId, target.id);
      stored = stored.filter((entry) => entry.id !== target.id);
      options.onChange?.();
      return new HttpResponse(null, { status: 204 });
    }),

    http.get(FIRST_TRADES_PATH, async ({ request }) => {
      const { failure } = await arrive('first-trades', request);
      return failure ?? HttpResponse.json(firstTrades);
    }),
  );

  return fake;
}
