import type { components } from '@/api/generated/schema';

import { NOW } from './fixtures';

/**
 * Fixture builders for the exchange endpoints: `GET /api/exchanges`,
 * `GET /api/exchanges/runs` and `POST /api/exchanges/sync`.
 *
 * Typed with the generated OpenAPI types, like `fixtures.ts`. **And more than
 * typed: every builder refuses a state the backend cannot write.** Spec 011's
 * closing section is the reason. Its fixtures were built from the response
 * type alone, and 467 tests passed over a combination of fields no backend
 * code path produces, while a combination it does produce went untested.
 *
 * So the rules below are read from the code that writes each field -
 * `services/exchanges.py`, `services/exchange_sync.py` and
 * `repositories/exchange_sync_runs.py` - and spec 016's section "What the
 * backend can write". A builder that throws is a test asking for the
 * impossible, which is a defect in the test.
 *
 * No field here is monetary, and no fixture carries a credential or an
 * address: the exchange endpoints return neither.
 */

type Schemas = components['schemas'];

export type ExchangeResponse = Schemas['ExchangeResponse'];
export type ExchangeListResponse = Schemas['ExchangeListResponse'];
export type ExchangeKey = Schemas['ExchangeKey'];
export type AccountSyncStatus = Schemas['AccountSyncStatus'];
export type ExchangeSyncErrorKind = Schemas['ExchangeSyncErrorKind'];
export type ExchangeLastError = Schemas['ExchangeLastErrorResponse'];
export type ExchangeSyncRunResponse = Schemas['ExchangeSyncRunResponse'];
export type ExchangeSyncRunListResponse = Schemas['ExchangeSyncRunListResponse'];
export type ExchangeSyncTriggeredResponse = Schemas['ExchangeSyncTriggeredResponse'];
export type ExchangeAccountOutcome = Schemas['ExchangeAccountOutcomeResponse'];
export type AccountOutcomeStatus = Schemas['AccountOutcomeStatus'];
export type SyncRunStatus = Schemas['SyncRunStatus'];
export type SyncTrigger = Schemas['SyncTrigger'];

/*
 * Every member of every union, written out by hand as a `Record` whose values
 * are ignored. A member added to the backend's enum fails `tsc` here until a
 * test names it, so no table below can quietly cover eight kinds out of nine.
 */

const EXCHANGE_KEYS_RECORD: Record<ExchangeKey, true> = { bingx: true, bitget: true };
export const ALL_EXCHANGE_KEYS = Object.keys(EXCHANGE_KEYS_RECORD) as ExchangeKey[];

const ACCOUNT_STATUSES_RECORD: Record<AccountSyncStatus, true> = {
  auth_failed: true,
  error: true,
  never_synced: true,
  ok: true,
};
export const ALL_ACCOUNT_STATUSES = Object.keys(ACCOUNT_STATUSES_RECORD) as AccountSyncStatus[];

const ERROR_KINDS_RECORD: Record<ExchangeSyncErrorKind, true> = {
  auth: true,
  conflict: true,
  insufficient_scope: true,
  internal: true,
  invalid_request: true,
  rate_limited: true,
  retention_window: true,
  schema: true,
  unavailable: true,
};
export const ALL_EXCHANGE_ERROR_KINDS = Object.keys(ERROR_KINDS_RECORD) as ExchangeSyncErrorKind[];

const RUN_STATUSES_RECORD: Record<SyncRunStatus, true> = {
  running: true,
  success: true,
  partial: true,
  failed: true,
  interrupted: true,
};
export const ALL_RUN_STATUSES = Object.keys(RUN_STATUSES_RECORD) as SyncRunStatus[];

const TRIGGERS_RECORD: Record<SyncTrigger, true> = {
  scheduled: true,
  manual: true,
  startup: true,
};
export const ALL_TRIGGERS = Object.keys(TRIGGERS_RECORD) as SyncTrigger[];

const OUTCOME_STATUSES_RECORD: Record<AccountOutcomeStatus, true> = {
  failed: true,
  skipped: true,
  success: true,
};
export const ALL_OUTCOME_STATUSES = Object.keys(OUTCOME_STATUSES_RECORD) as AccountOutcomeStatus[];

/** The two kinds that leave an account `auth_failed`; every other kind leaves it `error`. */
export type AuthErrorKind = Extract<ExchangeSyncErrorKind, 'auth' | 'insufficient_scope'>;
export const AUTH_ERROR_KINDS: readonly AuthErrorKind[] = ['auth', 'insufficient_scope'];

export type NonAuthErrorKind = Exclude<ExchangeSyncErrorKind, AuthErrorKind>;
export const NON_AUTH_ERROR_KINDS = ALL_EXCHANGE_ERROR_KINDS.filter(
  (kind): kind is NonAuthErrorKind => !(AUTH_ERROR_KINDS as readonly string[]).includes(kind),
);

function isAuthKind(kind: ExchangeSyncErrorKind): kind is AuthErrorKind {
  return (AUTH_ERROR_KINDS as readonly string[]).includes(kind);
}

/**
 * The venue names and variable names the spec's venue table fixes, written
 * out rather than imported, so a change on either side is a diff here.
 */
export const VENUE_NAMES: Readonly<Record<ExchangeKey, string>> = {
  bitget: 'Bitget',
  bingx: 'BingX',
};

export const VENUE_VARIABLES: Readonly<Record<ExchangeKey, readonly string[]>> = {
  bitget: [
    'PORTFOLIO_BITGET_API_KEY',
    'PORTFOLIO_BITGET_API_SECRET',
    'PORTFOLIO_BITGET_API_PASSPHRASE',
  ],
  bingx: ['PORTFOLIO_BINGX_API_KEY', 'PORTFOLIO_BINGX_API_SECRET'],
};

/*
 * Instants. Every page test runs under `NOW` (2026-09-24T12:00:00Z) from
 * `fixtures.ts`. The ones a relative time is read from sit on whole minutes,
 * so "15 minutes ago" is the same under a formatter that floors and one that
 * rounds. They carry microseconds, as the backend serialises them.
 */

/** The last successful sync: 15 minutes before `NOW`. */
export const LAST_SYNCED_AT = '2026-09-24T11:45:00.000000Z';
/** An older success, 3 days before `NOW`, kept by an account that has failed since. */
export const OLD_SYNCED_AT = '2026-09-21T12:00:00.000000Z';

/**
 * `PORTFOLIO_EXCHANGE_HISTORY_START=2026-07-01`, read at 00:00 UTC. Recent
 * enough that Bitget's 90-day retention does not cut it: the whole request is
 * held, so `effective_since` equals it and nothing is truncated.
 */
export const RECENT_REQUESTED_SINCE = '2026-07-01T00:00:00Z';

/** `PORTFOLIO_EXCHANGE_HISTORY_START=2026-01-01`: older than the venue keeps. */
export const OLD_REQUESTED_SINCE = '2026-01-01T00:00:00Z';

/**
 * Where the history held begins after the retention clamp: a whole
 * millisecond, as `clamp_to_retention` floors it, serialised with
 * microseconds. `.250` of a second, so the banner has to round it **up**.
 */
export const TRUNCATED_EFFECTIVE_SINCE = '2026-06-27T12:05:36.250000Z';
/** How the banner names {@link TRUNCATED_EFFECTIVE_SINCE}: the next whole second, in UTC. */
export const TRUNCATED_EFFECTIVE_SINCE_TEXT = 'Jun 27, 2026, 12:05:37 PM UTC';

/** A clamp that landed exactly on a whole second: nothing to round. */
export const WHOLE_SECOND_EFFECTIVE_SINCE = '2026-06-27T12:05:36Z';
export const WHOLE_SECOND_EFFECTIVE_SINCE_TEXT = 'Jun 27, 2026, 12:05:36 PM UTC';

/**
 * Details exactly as the backend builds them: `str()` of an exchange error -
 * a fixed per-class summary, the HTTP status and a digits-only venue code -
 * the count-only message of a conflict, or a type name for our own defect.
 * Never a trade id, a symbol or the venue's own text.
 */
export const DETAILS: Readonly<Record<ExchangeSyncErrorKind, string>> = {
  auth: 'The exchange refused the API key (HTTP 401, venue code 40037).',
  insufficient_scope:
    'The exchange accepted the API key but it lacks read permission (HTTP 403, venue code 40014).',
  rate_limited:
    'The exchange refused the request because it was asked too often (HTTP 429, venue code 429).',
  unavailable: 'The exchange could not be reached or did not answer (HTTP 503).',
  retention_window:
    'The exchange refused a window older than the history it keeps (HTTP 400, venue code 40034).',
  invalid_request: 'The exchange refused the request as invalid (HTTP 400, venue code 40019).',
  schema: 'The exchange answered in an unexpected shape: data.fillList[0].size is not a decimal.',
  conflict:
    '1 fill(s) in the page share a trade id with a stored fill but differ in their accounting fields.',
  internal: 'KeyError',
};

function fail(message: string): never {
  throw new Error(`Impossible exchange fixture: ${message}`);
}

function isIntegerAtLeastZero(value: number): boolean {
  return Number.isInteger(value) && value >= 0;
}

/** `effective_since > requested_since`, and false when either is unknown - the backend's rule. */
function derivedTruncated(requested: string | null, effective: string | null): boolean {
  if (requested === null || effective === null) {
    return false;
  }
  return Date.parse(effective) > Date.parse(requested);
}

/**
 * Throws unless `view` is a state `services/exchanges.py` can serve.
 *
 * Exported so a test that patches a fixture by hand - a poll moving a count -
 * can check that the patched state is still one the backend could write.
 */
export function assertWritableExchange(view: ExchangeResponse): ExchangeResponse {
  const key = view.exchange_key;

  if (view.syncing && !view.configured) {
    fail(
      `${key} is syncing but not configured. syncing is the coordinator's in-flight flag ` +
        'and configured: a venue without credentials is not in the run.',
    );
  }

  if ((view.requested_since === null) !== (view.effective_since === null)) {
    fail(`${key}: the plan sets requested_since and effective_since together.`);
  }

  if (
    view.requested_since !== null &&
    view.effective_since !== null &&
    Date.parse(view.effective_since) < Date.parse(view.requested_since)
  ) {
    fail(`${key}: the retention clamp only moves effective_since forward of requested_since.`);
  }

  if (view.history_truncated !== derivedTruncated(view.requested_since, view.effective_since)) {
    fail(`${key}: history_truncated is derived as effective_since > requested_since.`);
  }

  if (!isIntegerAtLeastZero(view.fills_stored) || !isIntegerAtLeastZero(view.pending_windows)) {
    fail(`${key}: counts are COUNT(*)s, so non-negative integers.`);
  }

  if ((view.fills_stored > 0 || view.pending_windows > 0) && view.requested_since === null) {
    fail(`${key}: the plan is committed before any window is queued or any fill stored.`);
  }

  const lastError = view.last_error;

  switch (view.status) {
    case 'ok':
      if (view.last_synced_at === null) {
        fail(`${key}: a success sets ok and last_synced_at together.`);
      }
      if (lastError !== null) {
        fail(`${key}: an ok account's latest attempted outcome is its success, so no last_error.`);
      }
      break;
    case 'never_synced':
      if (view.last_synced_at !== null) {
        fail(`${key}: last_synced_at is only ever set by a success, which sets ok.`);
      }
      if (lastError !== null) {
        fail(`${key}: a failure sets error or auth_failed, so a never_synced account has none.`);
      }
      break;
    case 'error':
      if (lastError === null) {
        fail(`${key}: an error status and its failed outcome are committed together.`);
      }
      if (isAuthKind(lastError.error_kind)) {
        fail(`${key}: an ${lastError.error_kind} failure sets auth_failed, not error.`);
      }
      break;
    case 'auth_failed':
      if (lastError !== null && !isAuthKind(lastError.error_kind)) {
        fail(
          `${key}: auth_failed comes only from auth or insufficient_scope, and a skip does ` +
            `not replace last_error, so ${lastError.error_kind} is impossible.`,
        );
      }
      break;
  }

  return view;
}

/**
 * A configured venue that synced successfully 15 minutes ago and holds its
 * whole requested history. Every override is checked by
 * {@link assertWritableExchange}; `history_truncated` is derived from the two
 * instants unless the override states it, and a stated value that disagrees
 * is refused.
 */
export function exchange(overrides: Partial<ExchangeResponse> = {}): ExchangeResponse {
  const requested =
    'requested_since' in overrides ? (overrides.requested_since ?? null) : RECENT_REQUESTED_SINCE;
  const effective =
    'effective_since' in overrides ? (overrides.effective_since ?? null) : requested;

  return assertWritableExchange({
    exchange_key: 'bitget',
    configured: true,
    status: 'ok',
    syncing: false,
    last_synced_at: LAST_SYNCED_AT,
    fills_stored: 1234,
    pending_windows: 0,
    last_error: null,
    ...overrides,
    requested_since: requested,
    effective_since: effective,
    history_truncated: overrides.history_truncated ?? derivedTruncated(requested, effective),
  });
}

/**
 * A configured venue with **no account row**: nothing has run since its
 * credentials were set. `never_synced`, every instant null, every count 0,
 * no `last_error`. Only `syncing` may vary: a run that has started but not yet
 * created the row still reports the venue as syncing.
 */
export function unsyncedExchange(
  exchangeKey: ExchangeKey = 'bitget',
  options: { readonly syncing?: boolean } = {},
): ExchangeResponse {
  return assertWritableExchange({
    exchange_key: exchangeKey,
    configured: true,
    status: 'never_synced',
    syncing: options.syncing ?? false,
    requested_since: null,
    effective_since: null,
    history_truncated: false,
    last_synced_at: null,
    fills_stored: 0,
    pending_windows: 0,
    last_error: null,
  });
}

export function lastError(
  kind: ExchangeSyncErrorKind,
  detail: string | null = DETAILS[kind],
): ExchangeLastError {
  return { error_kind: kind, detail };
}

/**
 * An account a venue refused. `kind` null is the hand-edited database the
 * spec allows for: `auth_failed` with no `last_error`.
 *
 * The default is the first-run shape: the key was refused while the plan was
 * being made (the symbols call), so nothing was planned and nothing stored.
 */
export function authFailedExchange(
  kind: AuthErrorKind | null,
  overrides: Partial<ExchangeResponse> = {},
): ExchangeResponse {
  return exchange({
    status: 'auth_failed',
    last_synced_at: null,
    requested_since: null,
    effective_since: null,
    fills_stored: 0,
    last_error: kind === null ? null : lastError(kind),
    ...overrides,
  });
}

/** An account whose latest attempt failed for a reason other than its key. */
export function erroredExchange(
  kind: NonAuthErrorKind,
  overrides: Partial<ExchangeResponse> = {},
): ExchangeResponse {
  return exchange({
    status: 'error',
    last_synced_at: OLD_SYNCED_AT,
    last_error: lastError(kind),
    ...overrides,
  });
}

/** A venue whose retention cut the requested history short. */
export function truncatedExchange(overrides: Partial<ExchangeResponse> = {}): ExchangeResponse {
  return exchange({
    requested_since: OLD_REQUESTED_SINCE,
    effective_since: TRUNCATED_EFFECTIVE_SINCE,
    ...overrides,
  });
}

/*
 * The run log.
 */

/** The run instants: the latest finished run started 20 minutes before `NOW`. */
export const EXCHANGE_RUN_STARTED_AT = '2026-09-24T11:40:00.000000Z';
/** A run in flight: started one minute before `NOW`. */
export const EXCHANGE_RUNNING_STARTED_AT = '2026-09-24T11:59:00.000000Z';
/** An older run: 3 hours before `NOW`. */
export const EXCHANGE_OLD_RUN_STARTED_AT = '2026-09-24T09:00:00.000000Z';

interface OutcomeCounts {
  readonly windows_completed?: number;
  readonly pages?: number;
  readonly fills_seen?: number;
  readonly fills_inserted?: number;
}

function assertWritableOutcome(outcome: ExchangeAccountOutcome): ExchangeAccountOutcome {
  const key = outcome.exchange_key;
  const counts = [
    outcome.windows_completed,
    outcome.pages,
    outcome.fills_seen,
    outcome.fills_inserted,
  ];

  if (!counts.every(isIntegerAtLeastZero)) {
    fail(`${key}: outcome counts are non-negative integers.`);
  }
  if (outcome.fills_inserted > outcome.fills_seen) {
    fail(`${key}: a fill is inserted only after it is seen.`);
  }

  switch (outcome.status) {
    case 'success':
      if (outcome.error_kind !== null || outcome.detail !== null) {
        fail(`${key}: a success carries no error.`);
      }
      break;
    case 'skipped':
      if (outcome.error_kind !== null || outcome.detail !== null) {
        fail(`${key}: a skip carries no error; last_error keeps the one that caused it.`);
      }
      if (counts.some((count) => count !== 0)) {
        fail(`${key}: a skipped account asked the venue nothing, so every count is 0.`);
      }
      break;
    case 'failed':
      if (outcome.error_kind === null || outcome.detail === null) {
        fail(`${key}: a failure this application writes always has a kind and a detail.`);
      }
      break;
  }

  return outcome;
}

export function accountSucceeded(
  exchangeKey: ExchangeKey,
  counts: OutcomeCounts = {},
): ExchangeAccountOutcome {
  return assertWritableOutcome({
    exchange_key: exchangeKey,
    status: 'success',
    windows_completed: 1,
    pages: 1,
    fills_seen: 0,
    fills_inserted: 0,
    error_kind: null,
    detail: null,
    ...counts,
  });
}

/** A failed account. The counts are what it stored before failing: each page is its own commit. */
export function accountFailed(
  exchangeKey: ExchangeKey,
  kind: ExchangeSyncErrorKind,
  options: OutcomeCounts & { readonly detail?: string } = {},
): ExchangeAccountOutcome {
  const { detail = DETAILS[kind], ...counts } = options;

  return assertWritableOutcome({
    exchange_key: exchangeKey,
    status: 'failed',
    windows_completed: 0,
    pages: 0,
    fills_seen: 0,
    fills_inserted: 0,
    error_kind: kind,
    detail,
    ...counts,
  });
}

/**
 * A failed outcome with no kind and no detail. `_sync_account` never writes
 * one, but the `error_kind` CHECK allows NULL and `_last_error_of` names the
 * case: a hand edit of the database. So the run log can meet it. The `POST`'s
 * summary cannot: it is built in memory by the run that failed the account.
 *
 * Deliberately not checked by `assertWritableOutcome`, which describes what
 * this application writes.
 */
export function handEditedFailure(exchangeKey: ExchangeKey): ExchangeAccountOutcome {
  return {
    exchange_key: exchangeKey,
    status: 'failed',
    windows_completed: 0,
    pages: 0,
    fills_seen: 0,
    fills_inserted: 0,
    error_kind: null,
    detail: null,
  };
}

/** An `auth_failed` account a scheduled or startup run skipped. */
export function accountSkipped(exchangeKey: ExchangeKey): ExchangeAccountOutcome {
  return assertWritableOutcome({
    exchange_key: exchangeKey,
    status: 'skipped',
    windows_completed: 0,
    pages: 0,
    fills_seen: 0,
    fills_inserted: 0,
    error_kind: null,
    detail: null,
  });
}

function assertAccountsWritable(
  trigger: SyncTrigger,
  accounts: readonly ExchangeAccountOutcome[],
): void {
  const keys = accounts.map((account) => account.exchange_key);

  if (new Set(keys).size !== keys.length) {
    fail('a run records at most one outcome per account.');
  }
  if (keys.some((key, index) => index > 0 && (keys[index - 1] ?? '') > key)) {
    fail('a run syncs its accounts by exchange_key, and lists them that way.');
  }
  if (trigger === 'manual' && accounts.some((account) => account.status === 'skipped')) {
    fail('only a scheduled or startup run skips an account; a manual run retries it.');
  }
}

function sum(accounts: readonly ExchangeAccountOutcome[], field: 'fills_seen' | 'fills_inserted') {
  return accounts.reduce((total, account) => total + account[field], 0);
}

function countOf(accounts: readonly ExchangeAccountOutcome[], status: AccountOutcomeStatus) {
  return accounts.filter((account) => account.status === status).length;
}

/** `_run_status`: success, partial or failed over the accounts that were **attempted**. */
function runStatusOf(accounts: readonly ExchangeAccountOutcome[]): SyncRunStatus {
  const attempted = accounts.filter((account) => account.status !== 'skipped');
  const failed = countOf(attempted, 'failed');

  if (attempted.length === 0 || failed === 0) {
    return 'success';
  }
  return failed === attempted.length ? 'failed' : 'partial';
}

export interface FinishedRunOptions {
  readonly run_id?: number;
  readonly trigger?: SyncTrigger;
  readonly started_at?: string;
  readonly duration_ms?: number;
  readonly accounts?: readonly ExchangeAccountOutcome[];
  /**
   * Only for a run with **no accounts**: `success` when there is no owner,
   * `failed` when there are two. With accounts, the status is derived, as
   * `_run_status` derives it, and cannot be stated.
   */
  readonly status?: 'success' | 'failed';
}

/**
 * A run `_finish` closed out. Every counter, both fill sums and the status are
 * derived from `accounts`, the way the backend derives them, so a test cannot
 * state a run whose numbers disagree with its accounts.
 */
export function finishedRun(options: FinishedRunOptions = {}): ExchangeSyncRunResponse {
  const trigger = options.trigger ?? 'scheduled';
  const accounts = [
    ...(options.accounts ?? [accountSucceeded('bitget', { fills_seen: 40, fills_inserted: 12 })]),
  ];
  const startedAt = options.started_at ?? EXCHANGE_RUN_STARTED_AT;
  const durationMs = options.duration_ms ?? 3_000;

  assertAccountsWritable(trigger, accounts);
  if (options.status !== undefined && accounts.length > 0) {
    fail('a run with accounts has the status its accounts give it.');
  }
  if (!isIntegerAtLeastZero(durationMs)) {
    fail('duration_ms is a monotonic difference in whole milliseconds.');
  }

  return {
    run_id: options.run_id ?? 7,
    trigger,
    status: options.status ?? runStatusOf(accounts),
    started_at: startedAt,
    finished_at: new Date(Date.parse(startedAt) + durationMs).toISOString(),
    duration_ms: durationMs,
    accounts_total: accounts.length,
    accounts_succeeded: countOf(accounts, 'success'),
    accounts_failed: countOf(accounts, 'failed'),
    accounts_skipped: countOf(accounts, 'skipped'),
    fills_seen: sum(accounts, 'fills_seen'),
    fills_inserted: sum(accounts, 'fills_inserted'),
    accounts,
  };
}

export interface OpenRunOptions {
  readonly run_id?: number;
  readonly trigger?: SyncTrigger;
  readonly started_at?: string;
  /** What `open_run` wrote: the number of configured venues. */
  readonly accounts_total?: number;
  /** The outcomes committed so far. Each account's outcome is its own commit. */
  readonly accounts?: readonly ExchangeAccountOutcome[];
}

function openRun(status: 'running' | 'interrupted', options: OpenRunOptions) {
  const trigger = options.trigger ?? 'scheduled';
  const accounts = [...(options.accounts ?? [])];
  const accountsTotal = options.accounts_total ?? 1;

  assertAccountsWritable(trigger, accounts);
  if (accounts.length > accountsTotal) {
    fail('a run records outcomes only for the accounts open_run counted.');
  }

  return {
    run_id: options.run_id ?? 8,
    trigger,
    status,
    started_at: options.started_at ?? EXCHANGE_RUNNING_STARTED_AT,
    finished_at: null,
    duration_ms: null,
    accounts_total: accountsTotal,
    // `open_run` writes zeros and only `finish_run` writes the counts, so a run
    // in flight - or one that died - says 0 whatever outcomes it has recorded.
    accounts_succeeded: 0,
    accounts_failed: 0,
    accounts_skipped: 0,
    fills_seen: sum(accounts, 'fills_seen'),
    fills_inserted: sum(accounts, 'fills_inserted'),
    accounts,
  } satisfies ExchangeSyncRunResponse;
}

/** A run still at `running`: no end, no duration, zero counters, the outcomes committed so far. */
export function runningExchangeRun(options: OpenRunOptions = {}): ExchangeSyncRunResponse {
  return openRun('running', options);
}

/**
 * A run the sweep marked `interrupted`. Unlike a balance run, it **can** carry
 * outcomes: each account's outcome was committed as that account finished.
 */
export function interruptedExchangeRun(options: OpenRunOptions = {}): ExchangeSyncRunResponse {
  return openRun('interrupted', { run_id: 6, started_at: EXCHANGE_OLD_RUN_STARTED_AT, ...options });
}

/**
 * What `POST /api/exchanges/sync` answers: a **finished** run, because the
 * request is held until the run ends, plus `joined`.
 *
 * An unjoined request started the run, so its trigger is `manual`. A joined
 * one reports the running run's trigger, whatever it was.
 */
export function syncTriggered(
  run: ExchangeSyncRunResponse = finishedRun({ trigger: 'manual' }),
  joined = false,
): ExchangeSyncTriggeredResponse {
  if (run.status === 'running' || run.status === 'interrupted') {
    fail('the sync endpoint answers when the run it started or joined has finished.');
  }
  if (!joined && run.trigger !== 'manual') {
    fail('a request that was not joined started the run itself, so the run is manual.');
  }
  return { ...run, joined };
}

/** The list endpoint's body. */
export function exchangeList(...exchanges: ExchangeResponse[]): ExchangeListResponse {
  const keys = exchanges.map((entry) => entry.exchange_key);
  if (new Set(keys).size !== keys.length) {
    fail('the list has one entry per venue.');
  }
  if (keys.some((key, index) => index > 0 && (keys[index - 1] ?? '') > key)) {
    fail('the list is sorted by exchange_key.');
  }
  return { exchanges };
}

/** The instant `NOW` names, re-exported so an exchange test imports its clock from one place. */
export { NOW };
