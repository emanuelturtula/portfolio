/**
 * The exchanges page's pure logic: venue names and the environment variables their
 * credentials come from, the status-label rule, every sentence `Record`, and the
 * remediation-step choice for an `auth_failed` account. See
 * docs/specs/016-exchanges-page.md.
 *
 * No React anywhere in this module - it is exercised directly by tests and by every
 * component under `src/pages/exchanges/`, the same split `src/lib/freshness.ts` and
 * `src/lib/chains.ts` use for the same reason.
 *
 * Every `Record` below is keyed by a generated union, so a member added to `ExchangeKey`,
 * `AccountSyncStatus`, `ExchangeSyncErrorKind`, `SyncRunStatus`, `SyncTrigger` or
 * `AccountOutcomeStatus` on the backend fails `tsc` here until it has an entry.
 */
import type { components } from '@/api/generated/schema';
import { formatDuration } from '@/lib/time';

export type ExchangeKey = components['schemas']['ExchangeKey'];
export type AccountSyncStatus = components['schemas']['AccountSyncStatus'];
export type ExchangeSyncErrorKind = components['schemas']['ExchangeSyncErrorKind'];
export type SyncRunStatus = components['schemas']['SyncRunStatus'];
export type SyncTrigger = components['schemas']['SyncTrigger'];
export type AccountOutcomeStatus = components['schemas']['AccountOutcomeStatus'];

export interface ExchangeInfo {
  readonly name: string;
  /**
   * The environment variable names credentials for this venue are read from - public in
   * docs/operations.md, never a value. No endpoint ever returns one, so nothing here can
   * leak a credential; see CLAUDE.md rule 3.
   */
  readonly variables: readonly string[];
}

/**
 * `bingx`'s variables are a guess at #14's naming, called out in the spec's "Handed on"
 * section: #14 must confirm them, or correct this table, once it configures the venue.
 */
export const EXCHANGES: Record<ExchangeKey, ExchangeInfo> = {
  bingx: {
    name: 'BingX',
    variables: ['PORTFOLIO_BINGX_API_KEY', 'PORTFOLIO_BINGX_API_SECRET'],
  },
  bitget: {
    name: 'Bitget',
    variables: [
      'PORTFOLIO_BITGET_API_KEY',
      'PORTFOLIO_BITGET_API_SECRET',
      'PORTFOLIO_BITGET_API_PASSPHRASE',
    ],
  },
};

export const STATUS_LABELS: Record<AccountSyncStatus, string> = {
  never_synced: 'Never synced',
  ok: 'Up to date',
  error: 'Sync failed',
  auth_failed: 'Authentication failed',
};

/**
 * The label for one account entry, in precedence order (spec R4 - review found the plain
 * `syncing`-overrides-everything rule wrong: a retried `auth_failed` venue must keep saying
 * so, not "Syncing", since a scheduled run *skips* it and only a manual one is actually
 * reading it):
 *
 * 1. `!configured` - "Not configured".
 * 2. `status === 'auth_failed'` - "Authentication failed", **even while `syncing`**.
 * 3. `syncing` - "Syncing".
 * 4. `status === 'ok'` and `pending_windows > 0` - "Unfinished".
 * 5. otherwise - `STATUS_LABELS[status]`.
 */
export function statusLabel(e: {
  readonly status: AccountSyncStatus;
  readonly configured: boolean;
  readonly syncing: boolean;
  readonly pending_windows: number;
}): string {
  if (!e.configured) {
    return 'Not configured';
  }
  if (e.status === 'auth_failed') {
    return STATUS_LABELS.auth_failed;
  }
  if (e.syncing) {
    return 'Syncing';
  }
  if (e.status === 'ok' && e.pending_windows > 0) {
    return 'Unfinished';
  }
  return STATUS_LABELS[e.status];
}

/**
 * One sentence per `ExchangeSyncErrorKind`, worded so the venue's name reads naturally in
 * it. `internal` names no venue: a defect on this side is not the venue's to be blamed for.
 */
const ERROR_KIND_SENTENCES: Record<ExchangeSyncErrorKind, (venue: string) => string> = {
  auth: (venue) => `${venue} refused the API key.`,
  insufficient_scope: (venue) => `The API key does not have read permission at ${venue}.`,
  rate_limited: (venue) => `${venue} throttled the requests for longer than the sync waits.`,
  unavailable: (venue) => `${venue} could not be reached, or answered that it was unavailable.`,
  retention_window: (venue) => `${venue} refused a window of history as older than it keeps.`,
  invalid_request: (venue) => `${venue} refused a request this application built.`,
  schema: (venue) => `${venue} answered in a shape this application could not read.`,
  conflict: (venue) =>
    `A fill ${venue} returned differs from the one stored under the same id. The sync stops at that page until someone looks.`,
  internal: () =>
    'A defect in this application stopped the sync. The container log has the details.',
};

/** The sentence for `kind`, naming `venue` where that reads naturally. */
export function errorSentence(kind: ExchangeSyncErrorKind, venue: string): string {
  return ERROR_KIND_SENTENCES[kind](venue);
}

export const RUN_STATUS_LABELS: Record<SyncRunStatus, string> = {
  running: 'Running',
  success: 'Succeeded',
  partial: 'Partially succeeded',
  failed: 'Failed',
  interrupted: 'Interrupted',
};

export const TRIGGER_LABELS: Record<SyncTrigger, string> = {
  scheduled: 'Scheduled',
  manual: 'Manual',
  startup: 'At startup',
};

export const OUTCOME_LABELS: Record<AccountOutcomeStatus, string> = {
  success: 'Succeeded',
  failed: 'Failed',
  skipped: 'Skipped',
};

/**
 * Which ordered remediation an `auth_failed` account needs. `null` for every other status -
 * `auth_failed` is the only status that needs the owner to act (criterion 2).
 *
 * **`'key'` whenever `!configured`, whatever `last_error.error_kind` says (spec R3):**
 * without credentials the Sync now button itself is hidden, so the credentials have to come
 * back before anything else does - pointing at the scope steps here would tell the owner to
 * press a button that is not on screen. Configured, it is `'key'` for an `auth` failure or a
 * missing `last_error` (a hand-edited row - see "What the backend can write"), and `'scope'`
 * for `insufficient_scope` - `auth_failed` never comes from any other `error_kind`, so these
 * are exhaustive for a configured account.
 */
export function remediationFor(e: {
  readonly status: AccountSyncStatus;
  readonly configured: boolean;
  readonly last_error: { readonly error_kind: ExchangeSyncErrorKind } | null;
}): 'key' | 'scope' | null {
  if (e.status !== 'auth_failed') {
    return null;
  }
  if (!e.configured) {
    return 'key';
  }
  return e.last_error?.error_kind === 'insufficient_scope' ? 'scope' : 'key';
}

/** A count - never money - formatted with grouping, e.g. "1,234". */
export function formatCount(n: number): string {
  return n.toLocaleString('en');
}

/**
 * A run's `duration_ms`, or "—" for a run still `running` or swept `interrupted` - both
 * leave it `null` (`ExchangeSyncRunResponse`'s own doc comment). Shared by `SyncRunTable`,
 * where both branches are reachable, and `SyncResult`, where the sync endpoint always
 * answers with a finished run - so only the non-null side ever actually runs there, and
 * sharing this one function is what keeps that a non-issue instead of an uncovered branch
 * duplicated in a second place.
 */
export function formatRunDuration(durationMs: number | null): string {
  return durationMs === null ? '—' : formatDuration(durationMs);
}

/**
 * Fallback for a `failed` account outcome whose `error_kind` is somehow absent - only
 * reachable through a hand-edited row, since the application always writes both fields
 * together for a failure (`assertWritableOutcome` in the tests refuses to construct any
 * other combination).
 */
export const UNKNOWN_ACCOUNT_FAILURE_MESSAGE = 'The sync could not read this account.';

/**
 * The sentence for a failed account outcome: `errorSentence` when `errorKind` is present,
 * {@link UNKNOWN_ACCOUNT_FAILURE_MESSAGE} otherwise. Shared by `SyncRunTable` (where a
 * hand-edited `null` `error_kind` is reachable) and `SyncResult` (where it never actually
 * occurs, for the same reason `formatRunDuration` gives).
 */
export function accountFailureSentence(
  errorKind: ExchangeSyncErrorKind | null,
  venue: string,
): string {
  return errorKind === null ? UNKNOWN_ACCOUNT_FAILURE_MESSAGE : errorSentence(errorKind, venue);
}
