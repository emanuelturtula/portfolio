import { describe, expect, it } from 'vitest';

import {
  errorSentence,
  EXCHANGES,
  formatCount,
  OUTCOME_LABELS,
  remediationFor,
  RUN_STATUS_LABELS,
  STATUS_LABELS,
  statusLabel,
  TRIGGER_LABELS,
} from '@/lib/exchanges';
import {
  ALL_ACCOUNT_STATUSES,
  ALL_EXCHANGE_ERROR_KINDS,
  ALL_EXCHANGE_KEYS,
  ALL_OUTCOME_STATUSES,
  ALL_RUN_STATUSES,
  ALL_TRIGGERS,
  authFailedExchange,
  erroredExchange,
  exchange,
  NON_AUTH_ERROR_KINDS,
  unsyncedExchange,
  VENUE_NAMES,
  VENUE_VARIABLES,
  type AccountSyncStatus,
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

describe('statusLabel', () => {
  it.each(ALL_ACCOUNT_STATUSES)(
    'labels a %s account that is not syncing by its status',
    (status) => {
      expect(statusLabel({ status, syncing: false })).toBe(STATUS_LABEL_TABLE[status]);
      expect(STATUS_LABELS[status]).toBe(STATUS_LABEL_TABLE[status]);
    },
  );

  it.each(ALL_ACCOUNT_STATUSES)('labels a %s account that is syncing as syncing', (status) => {
    // `syncing` replaces the label whatever the status: an auth_failed venue
    // being retried says Syncing, and keeps the rest of its entry.
    expect(statusLabel({ status, syncing: true })).toBe(SYNCING_LABEL);
  });

  it('gives every status, and syncing, a label of its own', () => {
    // Never colour alone: two statuses sharing a label would be told apart
    // only by styling, if at all.
    const labels = [...ALL_ACCOUNT_STATUSES.map((status) => STATUS_LABELS[status]), SYNCING_LABEL];

    expect(new Set(labels).size).toBe(labels.length);
  });

  it('reads a whole list entry', () => {
    expect(statusLabel(authFailedExchange('auth', { syncing: true }))).toBe(SYNCING_LABEL);
    expect(statusLabel(authFailedExchange('auth'))).toBe('Authentication failed');
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

  it('keeps the remediation after the credentials were removed', () => {
    expect(remediationFor(authFailedExchange('auth', { configured: false }))).toBe('key');
  });

  it('offers none for a status the owner does not have to act on', () => {
    expect(remediationFor(exchange())).toBeNull();
    expect(remediationFor(unsyncedExchange())).toBeNull();
    for (const kind of NON_AUTH_ERROR_KINDS) {
      expect(remediationFor(erroredExchange(kind))).toBeNull();
    }
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
