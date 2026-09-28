import { describe, expect, it } from 'vitest';

import {
  accountFailureSentence,
  errorSentence,
  EXCHANGES,
  formatCount,
  formatRunDuration,
  OUTCOME_LABELS,
  remediationFor,
  RUN_STATUS_LABELS,
  STATUS_LABELS,
  statusLabel,
  TRIGGER_LABELS,
  UNKNOWN_ACCOUNT_FAILURE_MESSAGE,
} from '@/lib/exchanges';
import {
  ALL_ACCOUNT_STATUSES,
  ALL_EXCHANGE_ERROR_KINDS,
  ALL_EXCHANGE_KEYS,
  ALL_OUTCOME_STATUSES,
  ALL_RUN_STATUSES,
  ALL_TRIGGERS,
  authFailedExchange,
  drainedBeforeMarkSynced,
  erroredExchange,
  exchange,
  NON_AUTH_ERROR_KINDS,
  unsyncedExchange,
  VENUE_NAMES,
  VENUE_VARIABLES,
  type AccountSyncStatus,
  type ExchangeResponse,
  type ExchangeSyncErrorKind,
  type SyncRunStatus,
  type SyncTrigger,
} from '@/test/exchangeFixtures';

/**
 * The spec's tables, written out literally rather than read back from the
 * module under test, so a wording change on either side is a diff here.
 */
const STATUS_LABEL_TABLE: Readonly<Record<AccountSyncStatus, string>> = {
  ok: 'Up to date',
  never_synced: 'Never synced',
  error: 'Sync failed',
  auth_failed: 'Authentication failed',
};

const SYNCING_LABEL = 'Syncing';

/** `ERROR_KIND_SENTENCES`, with `{Venue}` as a placeholder. */
const SENTENCE_TABLE: Readonly<Record<ExchangeSyncErrorKind, string>> = {
  auth: '{Venue} refused the API key.',
  insufficient_scope: 'The API key does not have read permission at {Venue}.',
  rate_limited: '{Venue} throttled the requests for longer than the sync waits.',
  unavailable: '{Venue} could not be reached, or answered that it was unavailable.',
  retention_window: '{Venue} refused a window of history as older than it keeps.',
  invalid_request: '{Venue} refused a request this application built.',
  schema: '{Venue} answered in a shape this application could not read.',
  conflict:
    'A fill {Venue} returned differs from the one stored under the same id. ' +
    'The sync stops at that page until someone looks.',
  internal: 'A defect in this application stopped the sync. The container log has the details.',
};

const RUN_STATUS_TABLE: Readonly<Record<SyncRunStatus, string>> = {
  running: 'Running',
  success: 'Succeeded',
  partial: 'Partially succeeded',
  failed: 'Failed',
  interrupted: 'Interrupted',
};

const TRIGGER_TABLE: Readonly<Record<SyncTrigger, string>> = {
  scheduled: 'Scheduled',
  manual: 'Manual',
  startup: 'At startup',
};

function sentenceFor(kind: ExchangeSyncErrorKind, venue: string): string {
  return SENTENCE_TABLE[kind].replaceAll('{Venue}', venue);
}

const NOT_CONFIGURED_LABEL = 'Not configured';
const UNFINISHED_LABEL = 'Unfinished';

/** An unconfigured account that was created by a run and never planned. */
const unconfiguredUnplanned = (): ExchangeResponse =>
  exchange({
    configured: false,
    status: 'never_synced',
    last_synced_at: null,
    requested_since: null,
    fills_stored: 0,
  });

/** R17: the owner's own retry, in flight. */
const RETRYING_LABEL = 'Retrying';

/** Whether the run log's newest run is `running` with `trigger: 'manual'`. */
type ManualRunInFlight = boolean;

/**
 * Spec 016 R4 and R17, the label rule in precedence order, one row per case
 * the backend can write. Each row names the rule that must win, and whether
 * the run log shows a manual run in flight. The two readings come from two
 * polls, so a row may pair a list and a log that disagree for a moment.
 */
const LABEL_CASES: readonly (readonly [
  string,
  () => ExchangeResponse,
  ManualRunInFlight,
  string,
])[] = [
  // 1. !configured wins over everything.
  ['unconfigured ok', () => exchange({ configured: false }), false, NOT_CONFIGURED_LABEL],
  [
    'unconfigured ok with windows pending',
    () => exchange({ configured: false, pending_windows: 4 }),
    false,
    NOT_CONFIGURED_LABEL,
  ],
  [
    'unconfigured auth_failed',
    () => authFailedExchange('auth', { configured: false }),
    false,
    NOT_CONFIGURED_LABEL,
  ],
  [
    'unconfigured auth_failed while a manual run retries the others',
    () => authFailedExchange('auth', { configured: false }),
    true,
    NOT_CONFIGURED_LABEL,
  ],
  [
    'unconfigured error',
    () => erroredExchange('unavailable', { configured: false }),
    false,
    NOT_CONFIGURED_LABEL,
  ],
  ['unconfigured never_synced', unconfiguredUnplanned, false, NOT_CONFIGURED_LABEL],
  // 2. auth_failed, syncing, and a manual run in flight: the owner's retry.
  [
    'auth_failed while a manual retry is syncing',
    () => authFailedExchange('auth', { syncing: true }),
    true,
    RETRYING_LABEL,
  ],
  [
    'auth_failed with insufficient scope while a manual retry is syncing',
    () => authFailedExchange('insufficient_scope', { syncing: true }),
    true,
    RETRYING_LABEL,
  ],
  [
    'auth_failed with no last error while a manual retry is syncing',
    () => authFailedExchange(null, { syncing: true }),
    true,
    RETRYING_LABEL,
  ],
  // 3. auth_failed otherwise, even while syncing.
  ['auth_failed', () => authFailedExchange('auth'), false, 'Authentication failed'],
  [
    'auth_failed while a scheduled or startup run is syncing',
    () => authFailedExchange('auth', { syncing: true }),
    false,
    'Authentication failed',
  ],
  [
    'auth_failed with insufficient scope while a scheduled run is syncing',
    () => authFailedExchange('insufficient_scope', { syncing: true }),
    false,
    'Authentication failed',
  ],
  [
    'auth_failed, a manual run in the log, the list not yet syncing',
    () => authFailedExchange('auth'),
    true,
    'Authentication failed',
  ],
  [
    'auth_failed drained before mark_synced',
    () => drainedBeforeMarkSynced(authFailedExchange('auth')),
    false,
    'Authentication failed',
  ],
  // 4. syncing, whoever started the run.
  ['ok, syncing', () => exchange({ syncing: true }), false, SYNCING_LABEL],
  ['ok, syncing in a manual run', () => exchange({ syncing: true }), true, SYNCING_LABEL],
  [
    'ok with windows pending, syncing',
    () => exchange({ syncing: true, pending_windows: 3 }),
    false,
    SYNCING_LABEL,
  ],
  ['error, syncing', () => erroredExchange('unavailable', { syncing: true }), false, SYNCING_LABEL],
  [
    'error, syncing in a manual run',
    () => erroredExchange('unavailable', { syncing: true }),
    true,
    SYNCING_LABEL,
  ],
  [
    'never_synced with no row, syncing',
    () => unsyncedExchange('bitget', { syncing: true }),
    false,
    SYNCING_LABEL,
  ],
  // 5. ok with windows pending.
  ['ok with windows pending', () => exchange({ pending_windows: 3 }), false, UNFINISHED_LABEL],
  ['ok with one window pending', () => exchange({ pending_windows: 1 }), false, UNFINISHED_LABEL],
  // 6. otherwise, the status.
  ['ok', () => exchange(), false, 'Up to date'],
  ['ok, a manual run in the log, the list not yet syncing', () => exchange(), true, 'Up to date'],
  ['never_synced with no row', () => unsyncedExchange('bitget'), false, 'Never synced'],
  [
    'never_synced with windows pending',
    () =>
      exchange({
        status: 'never_synced',
        last_synced_at: null,
        fills_stored: 40,
        pending_windows: 6,
      }),
    false,
    'Never synced',
  ],
  ['error with windows pending', () => erroredExchange('rate_limited'), false, 'Sync failed'],
  [
    'error from our own defect, nothing pending',
    () => erroredExchange('internal', { pending_windows: 0 }),
    false,
    'Sync failed',
  ],
];

describe('statusLabel', () => {
  // Reordered so the title's placeholders name the case, the flag and the
  // label, and not the builder function.
  it.each(
    LABEL_CASES.map(([name, build, manual, expected]) => [name, manual, expected, build] as const),
  )('labels %s (manual run in flight: %s) as %j', (_name, manual, expected, build) => {
    expect(statusLabel(build(), manual)).toBe(expected);
  });

  it.each(ALL_ACCOUNT_STATUSES)('keeps the stored label of %s in STATUS_LABELS', (status) => {
    expect(STATUS_LABELS[status]).toBe(STATUS_LABEL_TABLE[status]);
  });

  it('gives every label a text of its own', () => {
    // Never colour alone: two labels sharing a text would be told apart only
    // by styling, if at all.
    const labels = [
      ...ALL_ACCOUNT_STATUSES.map((status) => STATUS_LABELS[status]),
      SYNCING_LABEL,
      NOT_CONFIGURED_LABEL,
      UNFINISHED_LABEL,
      RETRYING_LABEL,
    ];

    expect(new Set(labels).size).toBe(labels.length);
    expect(new Set(LABEL_CASES.map(([, build, manual]) => statusLabel(build(), manual))).size).toBe(
      8,
    );
  });
});

describe('errorSentence', () => {
  it.each(ALL_EXCHANGE_ERROR_KINDS)('has the sentence for %s, naming the venue', (kind) => {
    for (const key of ALL_EXCHANGE_KEYS) {
      const venue = VENUE_NAMES[key];
      expect(errorSentence(kind, venue)).toBe(sentenceFor(kind, venue));
    }
  });

  it('names the venue it is given, not one written into the sentence', () => {
    expect(errorSentence('auth', 'Example Venue')).toBe('Example Venue refused the API key.');
    expect(errorSentence('insufficient_scope', 'Example Venue')).toBe(
      'The API key does not have read permission at Example Venue.',
    );
  });

  it('gives every kind a sentence of its own', () => {
    const sentences = ALL_EXCHANGE_ERROR_KINDS.map((kind) => errorSentence(kind, 'Bitget'));

    expect(new Set(sentences).size).toBe(sentences.length);
  });
});

describe('the venue table', () => {
  it.each(ALL_EXCHANGE_KEYS)('names %s and its credential variables', (key) => {
    expect(EXCHANGES[key].name).toBe(VENUE_NAMES[key]);
    expect([...EXCHANGES[key].variables]).toEqual(VENUE_VARIABLES[key]);
  });

  it('holds variable names only, never a value', () => {
    for (const key of ALL_EXCHANGE_KEYS) {
      for (const variable of EXCHANGES[key].variables) {
        expect(variable).toMatch(/^PORTFOLIO_[A-Z]+_API_[A-Z]+$/);
      }
    }
  });
});

describe('the run log labels', () => {
  it.each(ALL_RUN_STATUSES)('labels a %s run', (status) => {
    expect(RUN_STATUS_LABELS[status]).toBe(RUN_STATUS_TABLE[status]);
  });

  it.each(ALL_TRIGGERS)('labels a %s trigger', (trigger) => {
    expect(TRIGGER_LABELS[trigger]).toBe(TRIGGER_TABLE[trigger]);
  });

  it('gives every account outcome a label of its own', () => {
    // The spec leaves the words to the implementation; it fixes that they
    // exist and differ, because "Bitget: Failed" and "Bitget: Skipped" must
    // not read the same.
    const labels = ALL_OUTCOME_STATUSES.map((status) => OUTCOME_LABELS[status]);

    for (const label of labels) {
      expect(label.trim()).not.toBe('');
    }
    expect(new Set(labels).size).toBe(labels.length);
  });
});

describe('remediationFor', () => {
  it('chooses the key steps for a refused key', () => {
    expect(remediationFor(authFailedExchange('auth'))).toBe('key');
  });

  it('chooses the scope steps for a key without read permission', () => {
    expect(remediationFor(authFailedExchange('insufficient_scope'))).toBe('scope');
  });

  it('falls back to the key steps when auth_failed has no last error', () => {
    // A hand-edited database. The key steps are the ones that cover every
    // cause, so they are the safe default.
    expect(remediationFor(authFailedExchange(null))).toBe('key');
  });

  it('keeps the remediation while a retry of the venue is syncing', () => {
    // Until the run ends, the refusal is still the latest fact about the key.
    expect(remediationFor(authFailedExchange('auth', { syncing: true }))).toBe('key');
    expect(remediationFor(authFailedExchange('insufficient_scope', { syncing: true }))).toBe(
      'scope',
    );
  });

  it('keeps the remediation when every window was drained before mark_synced', () => {
    expect(remediationFor(drainedBeforeMarkSynced(authFailedExchange('auth')))).toBe('key');
    expect(remediationFor(drainedBeforeMarkSynced(authFailedExchange('insufficient_scope')))).toBe(
      'scope',
    );
  });

  it('gives an unconfigured venue the key steps, whatever the kind', () => {
    // R3: without credentials the Sync now button is hidden, so the scope
    // steps would point at a button that is not there. The credentials have
    // to come back first.
    expect(remediationFor(authFailedExchange('auth', { configured: false }))).toBe('key');
    expect(remediationFor(authFailedExchange('insufficient_scope', { configured: false }))).toBe(
      'key',
    );
    expect(remediationFor(authFailedExchange(null, { configured: false }))).toBe('key');
  });

  it('offers none for a status the owner does not have to act on', () => {
    expect(remediationFor(exchange())).toBeNull();
    expect(remediationFor(unsyncedExchange())).toBeNull();
    for (const kind of NON_AUTH_ERROR_KINDS) {
      expect(remediationFor(erroredExchange(kind))).toBeNull();
    }
  });
});

describe('accountFailureSentence', () => {
  it.each(ALL_EXCHANGE_ERROR_KINDS)('is the kind sentence for %s', (kind) => {
    expect(accountFailureSentence(kind, 'Bitget')).toBe(sentenceFor(kind, 'Bitget'));
  });

  it('invents no cause for a failure with no kind', () => {
    // A hand-edited row. Blaming "a defect in this application", or any
    // other kind, would be a claim the row does not make.
    const sentence = accountFailureSentence(null, 'Bitget');

    expect(sentence).toBe(UNKNOWN_ACCOUNT_FAILURE_MESSAGE);
    for (const kind of ALL_EXCHANGE_ERROR_KINDS) {
      expect(sentence).not.toBe(sentenceFor(kind, 'Bitget'));
    }
    expect(sentence).not.toMatch(/defect|refused|throttled|reached/i);
  });
});

describe('formatRunDuration', () => {
  it('renders a duration through formatDuration, and none as a dash', () => {
    expect(formatRunDuration(125_000)).toBe('2 minutes 5 seconds');
    expect(formatRunDuration(0)).toBe('under a second');
    expect(formatRunDuration(null)).toBe('\u2014');
  });
});

describe('formatCount', () => {
  it.each([
    [0, '0'],
    [1, '1'],
    [999, '999'],
    [1_000, '1,000'],
    [1_234, '1,234'],
    [1_234_567, '1,234,567'],
  ])('renders %i as %j', (count, expected) => {
    expect(formatCount(count)).toBe(expected);
  });
});
