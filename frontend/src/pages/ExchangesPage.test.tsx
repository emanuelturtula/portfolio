import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FAST_POLL_MS, SLOW_POLL_MS } from '@/api/exchanges';
import { OUTCOME_LABELS } from '@/lib/exchanges';
import {
  accountFailed,
  accountSkipped,
  accountSucceeded,
  authFailedExchange,
  DETAILS,
  erroredExchange,
  exchange,
  EXCHANGE_RUN_STARTED_AT,
  EXCHANGE_OLD_RUN_STARTED_AT,
  finishedRun,
  handEditedFailure,
  interruptedExchangeRun,
  LAST_SYNCED_AT,
  lastError,
  NOW,
  OLD_REQUESTED_SINCE,
  OLD_SYNCED_AT,
  RECENT_REQUESTED_SINCE,
  runningExchangeRun,
  syncTriggered,
  truncatedExchange,
  TRUNCATED_EFFECTIVE_SINCE,
  TRUNCATED_EFFECTIVE_SINCE_TEXT,
  unsyncedExchange,
  VENUE_VARIABLES,
  WHOLE_SECOND_EFFECTIVE_SINCE,
  WHOLE_SECOND_EFFECTIVE_SINCE_TEXT,
  type ExchangeResponse,
  type ExchangeSyncRunResponse,
} from '@/test/exchangeFixtures';
import {
  EXCHANGE_SYNC_PATH,
  EXCHANGES_PATH,
  fakeExchanges,
  type FakeExchanges,
  type FakeExchangesOptions,
} from '@/test/fakeExchanges';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';

/**
 * Every exchanges-page test runs under a fixed clock, faking `Date` only.
 *
 * `setTimeout` stays real: MSW answers through it, and so do `settle()` and
 * `waitFor`'s own timeout. A test that needs a poll to fire fakes
 * `setInterval` as well, which is what TanStack Query's `refetchInterval` and
 * `useNow`'s tick run on.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

/** Also fakes `setInterval`, so a test can fire a poll by moving the clock. */
function fakeIntervals(): void {
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
  vi.setSystemTime(new Date(NOW));
}

/** Moves the fake clock, firing any poll that falls due, and lets its answer land. */
async function advance(ms: number): Promise<void> {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
  await settle();
}

/*
 * The page's sentences, written out from the spec rather than imported from
 * the module under test, so a wording change on either side is a diff here.
 */

const notConfiguredLine = (venue: string): string =>
  `No credentials for ${venue} are configured on the host. ` +
  'The fills already imported are kept, and nothing new is read.';
const syncingLine = (venue: string): string => `A sync is reading ${venue} now.`;
const RETRIES_LINE = 'The next scheduled sync tries again.';
const windowsPendingLine = (count: number): string =>
  `${String(count)} windows of history are still to read. The next sync continues from them.`;
const neverSyncedLine = (venue: string): string =>
  `No sync has finished for ${venue} yet. ` +
  'The next scheduled sync imports its history, or press Sync now.';

const PENDING_LINE = 'Syncing exchanges… the first import can take several minutes.';
const JOINED_LINE = 'It joined a sync that was already running.';
const skippedLine = (venue: string): string =>
  `${venue} was skipped, because its key was refused earlier and only a sync you start ` +
  'retries it. Press Sync now again to retry it.';
const FAILURE_PREFIX = 'The sync did not complete:';
const MAY_STILL_RUN =
  'A sync may still be running on the server; this page updates when it finishes.';

const bannerLine = (venue: string, when: string): string =>
  `${venue} does not return trades older than its retention window, so the history ` +
  `imported here is complete only from ${when}. Any trade made before then may be missing.`;
const bannerPendingLine = (count: number): string =>
  'The import has not finished. That is where the history will be complete from once ' +
  `the ${String(count)} windows still to read are read.`;

const EMPTY_TITLE = 'No exchange connected';
const EMPTY_TEXT =
  'Exchange API keys are read from environment variables on the host, for example ' +
  'PORTFOLIO_BITGET_API_KEY, and are never entered in this app. docs/operations.md, ' +
  'section 12, explains how to create a read-only key and where to put it.';
const NO_RUNS_LINE = 'No exchange sync has run yet.';

const keySteps = (venue: string): readonly string[] => [
  `At ${venue}, check that the API key still exists, or create a new read-only key. ` +
    'If the key has an IP allowlist, it must include the address the host reaches the ' +
    'internet from.',
  'Put the values in secrets.env on the host',
  'Recreate the container with docker compose up --force-recreate. ' +
    'A restart does not re-read secrets.env.',
  `Press Sync now. Scheduled syncs skip ${venue} until a sync you start succeeds.`,
];
const scopeSteps = (venue: string): readonly string[] => [
  `At ${venue}, edit the API key and grant read permission. Grant nothing else, and never ` +
    'trade, transfer or withdrawal.',
  `Press Sync now. Scheduled syncs skip ${venue} until a sync you start succeeds.`,
];
const NEW_KEY_NOTE = 'A new key instead needs steps 2 and 3 above first.';
const FULL_PROCEDURE = 'docs/operations.md, section 13, has the full procedure.';

/** The `ERROR_KIND_SENTENCES` the tests below meet, for Bitget and BingX. */
const SENTENCES = {
  bitgetUnavailable: 'Bitget could not be reached, or answered that it was unavailable.',
  bitgetRateLimited: 'Bitget throttled the requests for longer than the sync waits.',
  bitgetAuth: 'Bitget refused the API key.',
  bitgetScope: 'The API key does not have read permission at Bitget.',
  bingxConflict:
    'A fill BingX returned differs from the one stored under the same id. ' +
    'The sync stops at that page until someone looks.',
  bingxSchema: 'BingX answered in a shape this application could not read.',
} as const;

/** Two full stops in a row: a detail sentence glued to a template's own ".". */
const DOUBLE_PERIOD = /\.\s*\./;

/*
 * Locators.
 */

interface Setup {
  readonly user: UserEvent;
  readonly fake: FakeExchanges;
}

/**
 * Signs in, installs the fake exchange backend and opens `/exchanges`.
 * `overrides` take precedence over the fake and are in place before the first
 * render.
 */
function openExchanges(
  options: FakeExchangesOptions = { exchanges: [exchange()], runs: [finishedRun()] },
  overrides: readonly HttpHandler[] = [],
): Setup {
  const user = userEvent.setup();
  const session = fakeSession({ initialUser: TEST_USERNAME });
  const fake = fakeExchanges({ session, ...options });
  server.use(...session.handlers, ...fake.handlers);
  server.use(...overrides);

  renderApp(['/exchanges']);

  return { user, fake };
}

function sectionOf(heading: HTMLElement): HTMLElement {
  const section = heading.closest('section');
  if (section === null) {
    throw new Error(`"${heading.textContent}" does not head a section.`);
  }
  return section;
}

async function accountsSection(): Promise<HTMLElement> {
  return sectionOf(await screen.findByRole('heading', { level: 3, name: 'Accounts' }));
}

async function historySection(): Promise<HTMLElement> {
  return sectionOf(await screen.findByRole('heading', { level: 3, name: 'Sync history' }));
}

/** The list entry labelled by `name`, a venue's display name. */
async function venue(name: string): Promise<HTMLElement> {
  return within(await accountsSection()).findByRole('listitem', { name });
}

/** The banner headed "{name} history is incomplete". */
function banner(name: string): HTMLElement {
  return sectionOf(
    screen.getByRole('heading', { level: 3, name: `${name} history is incomplete` }),
  );
}

function syncButton(): HTMLElement {
  return screen.getByRole('button', { name: 'Sync now' });
}

/** The polite live region holding the last sync's summary. */
async function resultBlock(): Promise<HTMLElement> {
  return waitFor(() => {
    const found = screen
      .getAllByRole('status')
      .find((element) => element.textContent.includes('The sync '));
    if (found === undefined) {
      throw new Error('No sync result is on screen.');
    }
    return found;
  });
}

/** The ordered remediation steps of an entry, which must have them. */
function remediationSteps(item: HTMLElement): HTMLElement[] {
  const list = item.querySelector('ol');
  if (list === null) {
    throw new Error('The entry has no ordered list of steps.');
  }
  return within(list).getAllByRole('listitem');
}

/** The one `<time>` in `container`. */
function timeIn(container: HTMLElement): HTMLElement {
  const found = container.querySelectorAll('time');
  const only = found[0];
  if (found.length !== 1 || only === undefined) {
    throw new Error(`Expected one time element, found ${String(found.length)}.`);
  }
  return only;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Asserts a fact of an entry: its label, then its value, with nothing but
 * punctuation or spacing between them. "Fills stored 1,234" and
 * "Fills stored: 1,234" both pass; "Fills stored 11,234" does not.
 */
function expectFact(item: HTMLElement, label: string, value: string): void {
  expect(item).toHaveTextContent(
    new RegExp(`${escapeRegExp(label)}\\W*${escapeRegExp(value)}(?![\\d,])`, 'i'),
  );
}

const FACT = {
  lastSync: 'Last complete sync',
  fills: 'Fills stored',
  history: 'History complete from',
  windows: 'Windows still to read',
} as const;

/** The index of `text` in `container`'s text, which must contain it. */
function positionOf(container: HTMLElement, text: string): number {
  const index = container.textContent.replace(/\s+/g, ' ').indexOf(text);
  expect(index, `"${text}" is not in the entry`).toBeGreaterThanOrEqual(0);
  return index;
}

async function runTable(): Promise<HTMLTableElement> {
  const table = await within(await historySection()).findByRole('table');
  if (!(table instanceof HTMLTableElement)) {
    throw new Error('The run log is not a table.');
  }
  return table;
}

function bodyRows(table: HTMLTableElement): HTMLTableRowElement[] {
  return Array.from(table.tBodies).flatMap((body) => Array.from(body.rows));
}

function cell(row: HTMLTableRowElement, column: string): HTMLTableCellElement {
  const table = row.closest('table');
  const headers = Array.from(table?.tHead?.rows[0]?.cells ?? []);
  const index = headers.findIndex((header) => header.textContent.trim() === column);
  const found = row.cells[index];
  if (index < 0 || found === undefined) {
    throw new Error(`The run log has no "${column}" column.`);
  }
  return found;
}

function text(element: HTMLElement): string {
  return element.textContent.replace(/\s+/g, ' ').trim();
}

/** A manual run that read Bitget and BingX: 15 new fills of 45 read, in 3 seconds. */
function manualRunOverBoth(): ExchangeSyncRunResponse {
  return finishedRun({
    run_id: 9,
    trigger: 'manual',
    started_at: '2026-09-24T11:59:57.000000Z',
    duration_ms: 3_000,
    accounts: [
      accountSucceeded('bingx', { fills_seen: 5, fills_inserted: 3 }),
      accountSucceeded('bitget', { fills_seen: 40, fills_inserted: 12 }),
    ],
  });
}

function bothVenues(overrides: Partial<ExchangeResponse> = {}): ExchangeResponse[] {
  return [
    exchange({ exchange_key: 'bingx', fills_stored: 17, ...overrides }),
    exchange({ exchange_key: 'bitget', ...overrides }),
  ];
}

/*
 * Criterion 1: every status, distinct in text.
 */

describe('ExchangesPage: statuses', () => {
  it('an ok venue says up to date, when it last synced and what it holds', async () => {
    openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Up to date');
    expectFact(item, FACT.lastSync, '15 minutes ago');
    const synced = within(item).getByText('15 minutes ago');
    expect(synced.tagName).toBe('TIME');
    expect(synced).toHaveAttribute('dateTime', LAST_SYNCED_AT);
    expect(synced.getAttribute('title')).toBeTruthy();
    // A count, grouped the English way; not money, and not a bare number.
    expectFact(item, FACT.fills, '1,234');
    // The whole requested history is held, from 1 July.
    expectFact(item, FACT.history, 'Jul 1, 2026, 12:00:00 AM UTC');
    // Nothing is wrong, so nothing is said to be.
    expect(item).not.toHaveTextContent(/still to read/i);
    expect(item).not.toHaveTextContent(/detail:/i);
    expect(item).not.toHaveTextContent(RETRIES_LINE);
    expect(item).not.toHaveTextContent(/no credentials/i);
    expect(item).not.toHaveTextContent(/syncing/i);
    expect(item.querySelector('ol')).toBeNull();
  });

  it('a never-synced configured venue with no row says no sync has finished', async () => {
    openExchanges({ exchanges: [unsyncedExchange('bitget')], runs: [] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Never synced');
    expectFact(item, FACT.lastSync, 'Never');
    expectFact(item, FACT.fills, '0');
    expectFact(item, FACT.history, 'Not planned yet');
    expect(item).toHaveTextContent(neverSyncedLine('Bitget'));
    // Every instant is null, so no time element can be rendered from one.
    expect(item.querySelector('time')).toBeNull();
    expect(item).not.toHaveTextContent(/still to read/i);
    expect(item).not.toHaveTextContent(/detail:/i);
    expect(item).not.toHaveTextContent(RETRIES_LINE);
  });

  it('a never-synced venue with fills and windows pending shows both', async () => {
    // A first backfill interrupted mid-account: its pages are committed, its
    // status is not, because an interruption changes no status.
    openExchanges({
      exchanges: [
        exchange({
          status: 'never_synced',
          last_synced_at: null,
          fills_stored: 2_500,
          pending_windows: 6,
        }),
      ],
      runs: [interruptedExchangeRun({ accounts_total: 1 })],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Never synced');
    expectFact(item, FACT.lastSync, 'Never');
    expectFact(item, FACT.fills, '2,500');
    expectFact(item, FACT.windows, '6');
    expect(item).toHaveTextContent(windowsPendingLine(6));
    expect(item).toHaveTextContent(neverSyncedLine('Bitget'));
    // In the spec's order: the windows before the never-synced line.
    expect(positionOf(item, windowsPendingLine(6))).toBeLessThan(
      positionOf(item, neverSyncedLine('Bitget')),
    );
  });

  it("an error venue shows the kind's sentence, the detail and that the next sync retries", async () => {
    openExchanges({ exchanges: [erroredExchange('unavailable')], runs: [] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Sync failed');
    expect(item).toHaveTextContent(SENTENCES.bitgetUnavailable);
    expect(item).toHaveTextContent(`Detail: ${DETAILS.unavailable}`);
    expect(item).toHaveTextContent(RETRIES_LINE);
    // A failure leaves last_synced_at alone: the last success, three days ago, still stands.
    expectFact(item, FACT.lastSync, '3 days ago');
    expect(within(item).getByText('3 days ago')).toHaveAttribute('dateTime', OLD_SYNCED_AT);
    // Only auth_failed gets steps; an outage fixes itself.
    expect(item.querySelector('ol')).toBeNull();
    expect(item).not.toHaveTextContent(/secrets\.env/);
    expect(positionOf(item, SENTENCES.bitgetUnavailable)).toBeLessThan(
      positionOf(item, RETRIES_LINE),
    );
  });

  it('an error venue with no detail shows its sentence alone', async () => {
    openExchanges({
      exchanges: [erroredExchange('rate_limited', { last_error: lastError('rate_limited', null) })],
      runs: [],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent(SENTENCES.bitgetRateLimited);
    expect(item).not.toHaveTextContent(/detail:/i);
    expect(item).not.toHaveTextContent(/null/);
  });

  it('a syncing venue is labelled syncing and keeps its error', async () => {
    // A manual retry of a refused key: syncing replaces the label and nothing
    // else, because until the run ends the refusal is still the latest fact.
    openExchanges({
      exchanges: [authFailedExchange('auth', { syncing: true })],
      runs: [runningExchangeRun({ trigger: 'manual', accounts_total: 1 })],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Syncing');
    expect(item).not.toHaveTextContent('Authentication failed');
    expect(item).toHaveTextContent(syncingLine('Bitget'));
    expect(item).toHaveTextContent(SENTENCES.bitgetAuth);
    expect(item).toHaveTextContent(`Detail: ${DETAILS.auth}`);
    const steps = remediationSteps(item);
    expect(steps).toHaveLength(4);
    // In the spec's order: syncing, then the error, then the remediation.
    expect(positionOf(item, syncingLine('Bitget'))).toBeLessThan(
      positionOf(item, SENTENCES.bitgetAuth),
    );
    expect(positionOf(item, SENTENCES.bitgetAuth)).toBeLessThan(
      positionOf(item, keySteps('Bitget')[0] ?? ''),
    );
  });

  it('a syncing error venue does not promise the next scheduled sync', async () => {
    // The retry is happening now; "the next scheduled sync tries again" and
    // "the next sync continues" are for a venue left waiting.
    openExchanges({
      exchanges: [erroredExchange('unavailable', { syncing: true, pending_windows: 3 })],
      runs: [runningExchangeRun()],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Syncing');
    expect(item).toHaveTextContent(SENTENCES.bitgetUnavailable);
    expect(item).not.toHaveTextContent(RETRIES_LINE);
    expect(item).not.toHaveTextContent(windowsPendingLine(3));
    // The count itself still shows: it is what moves while the sync runs.
    expectFact(item, FACT.windows, '3');
  });

  it('a syncing venue with no row yet does not say no sync has finished', async () => {
    openExchanges({
      exchanges: [unsyncedExchange('bitget', { syncing: true })],
      runs: [runningExchangeRun()],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Syncing');
    expect(item).toHaveTextContent(syncingLine('Bitget'));
    expect(item).not.toHaveTextContent(neverSyncedLine('Bitget'));
  });

  it('an unconfigured venue says its credentials are gone and keeps its counts', async () => {
    openExchanges({
      exchanges: [exchange({ exchange_key: 'bitget', configured: false, fills_stored: 4_321 })],
      runs: [],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent(notConfiguredLine('Bitget'));
    // The row keeps its last state.
    expect(item).toHaveTextContent('Up to date');
    expectFact(item, FACT.fills, '4,321');
    expectFact(item, FACT.lastSync, '15 minutes ago');
    expect(item).not.toHaveTextContent(/syncing/i);
  });

  it('the not-configured line comes first', async () => {
    openExchanges({
      exchanges: [authFailedExchange('auth', { configured: false })],
      runs: [],
    });

    const item = await venue('Bitget');

    expect(positionOf(item, notConfiguredLine('Bitget'))).toBeLessThan(
      positionOf(item, SENTENCES.bitgetAuth),
    );
  });

  it('an ok venue with windows pending says the next sync continues from them', async () => {
    // Ok from an earlier run; a later run was interrupted with work queued.
    openExchanges({
      exchanges: [exchange({ pending_windows: 3 })],
      runs: [interruptedExchangeRun({ accounts_total: 1 })],
    });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Up to date');
    expectFact(item, FACT.windows, '3');
    expect(item).toHaveTextContent(windowsPendingLine(3));
    expect(item).not.toHaveTextContent(neverSyncedLine('Bitget'));
  });

  it('lists every venue the backend returns, each labelled by its name', async () => {
    openExchanges({
      exchanges: [authFailedExchange('insufficient_scope', { exchange_key: 'bingx' }), exchange()],
      runs: [],
    });

    const section = await accountsSection();
    await within(section).findByRole('listitem', { name: 'Bitget' });

    const bingx = within(section).getByRole('listitem', { name: 'BingX' });
    expect(bingx).toHaveTextContent('Authentication failed');
    expect(within(section).getByRole('listitem', { name: 'Bitget' })).toHaveTextContent(
      'Up to date',
    );
  });

  it('the last complete sync advances without a reload', async () => {
    fakeIntervals();
    openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });

    const item = await venue('Bitget');
    expectFact(item, FACT.lastSync, '15 minutes ago');

    await advance(SLOW_POLL_MS);

    await waitFor(() => {
      expectFact(item, FACT.lastSync, '16 minutes ago');
    });
  });
});

/*
 * Criterion 2: auth_failed gets a concrete remediation.
 */

describe('ExchangesPage: remediation', () => {
  it("auth_failed with an auth error lists the four steps and the venue's variables", async () => {
    openExchanges({ exchanges: [authFailedExchange('auth')], runs: [] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Authentication failed');
    expect(item).toHaveTextContent(SENTENCES.bitgetAuth);
    // Ordered: the steps only work in this order.
    const steps = remediationSteps(item);
    expect(steps).toHaveLength(4);
    keySteps('Bitget').forEach((step, index) => {
      expect(steps[index]).toHaveTextContent(step);
    });
    // Each variable name, as code, in the secrets.env step.
    const secretsStep = steps[1];
    if (secretsStep === undefined) {
      throw new Error('There is no second step.');
    }
    for (const variable of VENUE_VARIABLES.bitget) {
      expect(within(secretsStep).getByText(variable).tagName).toBe('CODE');
    }
    expect(within(steps[2] ?? item).getByText(/docker compose up --force-recreate/)).toBeTruthy();
    expect(item).toHaveTextContent(FULL_PROCEDURE);
    // Recreate, never restart: env_file is read when the container is created.
    expect(item).not.toHaveTextContent(/restart the container/i);
  });

  it('names BingX and its own variables for a refused BingX key', async () => {
    openExchanges({ exchanges: [authFailedExchange('auth', { exchange_key: 'bingx' })], runs: [] });

    const item = await venue('BingX');
    const steps = remediationSteps(item);

    keySteps('BingX').forEach((step, index) => {
      expect(steps[index]).toHaveTextContent(step);
    });
    for (const variable of VENUE_VARIABLES.bingx) {
      expect(within(item).getByText(variable).tagName).toBe('CODE');
    }
    expect(item).not.toHaveTextContent(/BITGET/);
  });

  it('auth_failed with insufficient scope asks for read permission', async () => {
    openExchanges({ exchanges: [authFailedExchange('insufficient_scope')], runs: [] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent(SENTENCES.bitgetScope);
    const steps = remediationSteps(item);
    expect(steps).toHaveLength(2);
    scopeSteps('Bitget').forEach((step, index) => {
      expect(steps[index]).toHaveTextContent(step);
    });
    expect(item).toHaveTextContent(NEW_KEY_NOTE);
    expect(item).toHaveTextContent(FULL_PROCEDURE);
    // The key exists and works; replacing it is not the fix.
    expect(item).not.toHaveTextContent(/check that the API key still exists/);
  });

  it('auth_failed with no last error falls back to the key steps', async () => {
    openExchanges({ exchanges: [authFailedExchange(null)], runs: [] });

    const item = await venue('Bitget');

    expect(item).toHaveTextContent('Authentication failed');
    expect(item).not.toHaveTextContent(/detail:/i);
    const steps = remediationSteps(item);
    expect(steps).toHaveLength(4);
    keySteps('Bitget').forEach((step, index) => {
      expect(steps[index]).toHaveTextContent(step);
    });
  });

  it('auth_failed is not told the next scheduled sync retries it', async () => {
    // Scheduled runs skip an auth_failed account; only a sync the owner starts
    // retries it, and the last step says exactly that.
    openExchanges({ exchanges: [authFailedExchange('auth')], runs: [] });

    const item = await venue('Bitget');

    expect(item).not.toHaveTextContent(RETRIES_LINE);
  });
});

/*
 * Criterion 3: Sync now, with progress and a result.
 */

describe('ExchangesPage: Sync now', () => {
  it('Sync now shows a pending line and disables itself while its request is in flight', async () => {
    const { user, fake } = openExchanges({
      exchanges: [exchange()],
      runs: [finishedRun()],
      onSync: (exchanges) => {
        const run = manualRunOverBoth();
        exchanges.setRuns([run, ...exchanges.runs()]);
        return syncTriggered(run);
      },
    });
    await venue('Bitget');
    const release = fake.hold('sync');
    const button = syncButton();
    expect(screen.queryByText(PENDING_LINE)).not.toBeInTheDocument();

    await user.click(button);

    await waitFor(() => {
      expect(button).toBeDisabled();
    });
    const pending = await screen.findByText(PENDING_LINE);
    expect(pending.closest('[role="status"]')).not.toBeNull();
    // A second press while the first is in flight sends nothing.
    await user.click(button);
    expect(fake.requestsTo('sync')).toHaveLength(1);
    // Bodyless, and still declared JSON, or the backend's write guard refuses it.
    expect(fake.requestsTo('sync')[0]?.contentType).toBe('application/json');

    release();

    await waitFor(() => {
      expect(button).toBeEnabled();
    });
    expect(screen.queryByText(PENDING_LINE)).not.toBeInTheDocument();
    expect(await resultBlock()).toHaveTextContent(/The sync succeeded:/);
    expect(fake.requestsTo('sync')).toHaveLength(1);
  });

  it("while the sync runs, the venue's fills stored and windows left update from the poll", async () => {
    fakeIntervals();
    const { user, fake } = openExchanges({
      exchanges: [unsyncedExchange('bitget')],
      runs: [],
      onSync: (exchanges) => {
        const run = finishedRun({
          run_id: 1,
          trigger: 'manual',
          started_at: '2026-09-24T12:00:00.000000Z',
          duration_ms: 12_000,
          accounts: [accountSucceeded('bitget', { fills_seen: 400, fills_inserted: 400 })],
        });
        exchanges.setExchanges([
          exchange({ fills_stored: 400, pending_windows: 0, last_synced_at: NOW }),
        ]);
        exchanges.setRuns([run]);
        return syncTriggered(run);
      },
    });
    const item = await venue('Bitget');
    expectFact(item, FACT.fills, '0');
    const release = fake.hold('sync');

    await user.click(syncButton());
    await screen.findByText(PENDING_LINE);

    // The run has planned seven windows and committed its first page.
    fake.setExchanges([
      exchange({
        status: 'never_synced',
        syncing: true,
        last_synced_at: null,
        fills_stored: 100,
        pending_windows: 7,
      }),
    ]);
    await advance(FAST_POLL_MS);

    await waitFor(() => {
      expectFact(item, FACT.fills, '100');
    });
    expectFact(item, FACT.windows, '7');
    expect(item).toHaveTextContent('Syncing');

    // Five seconds later, more pages and fewer windows.
    fake.patchExchange('bitget', { fills_stored: 1_250, pending_windows: 3 });
    await advance(FAST_POLL_MS);

    await waitFor(() => {
      expectFact(item, FACT.fills, '1,250');
    });
    expectFact(item, FACT.windows, '3');

    release();

    await waitFor(() => {
      expectFact(item, FACT.fills, '400');
    });
    expect(item).toHaveTextContent('Up to date');
    expect(item).not.toHaveTextContent(/still to read/i);
  });

  it('a running run appears in the history while the sync is pending', async () => {
    fakeIntervals();
    const { user, fake } = openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    await venue('Bitget');
    const release = fake.hold('sync');

    await user.click(syncButton());
    await screen.findByText(PENDING_LINE);
    fake.setRuns([
      runningExchangeRun({
        run_id: 8,
        trigger: 'manual',
        started_at: NOW,
        accounts_total: 1,
      }),
      finishedRun(),
    ]);
    await advance(FAST_POLL_MS);

    const table = await runTable();
    await waitFor(() => {
      expect(bodyRows(table)).toHaveLength(2);
    });
    const newest = bodyRows(table)[0];
    if (newest === undefined) {
      throw new Error('The run log has no rows.');
    }
    expect(text(cell(newest, 'Status'))).toBe('Running');
    expect(text(cell(newest, 'Trigger'))).toBe('Manual');
    expect(text(cell(newest, 'Duration'))).toBe('—');

    release();
  });

  it('the result summarises new and read fills, exchanges and duration', async () => {
    const { user } = openExchanges({
      exchanges: bothVenues(),
      runs: [],
      onSync: (exchanges) => {
        const run = manualRunOverBoth();
        exchanges.setRuns([run]);
        return syncTriggered(run);
      },
    });
    await venue('Bitget');

    await user.click(syncButton());

    const result = await resultBlock();
    expect(result).toHaveTextContent(
      'The sync succeeded: 15 new fills (45 read) from 2 exchanges, in 3 seconds.',
    );
    expect(result).not.toHaveTextContent(JOINED_LINE);
    expect(result).not.toHaveTextContent(/skipped/i);
  });

  it('the result groups large counts and uses the singular for one', async () => {
    const { user } = openExchanges({
      exchanges: [exchange()],
      runs: [],
      onSync: () =>
        syncTriggered(
          finishedRun({
            trigger: 'manual',
            duration_ms: 400,
            accounts: [accountSucceeded('bitget', { fills_seen: 1, fills_inserted: 1 })],
          }),
        ),
    });
    await venue('Bitget');

    await user.click(syncButton());

    expect(await resultBlock()).toHaveTextContent(
      'The sync succeeded: 1 new fill (1 read) from 1 exchange, in under a second.',
    );
  });

  it('the result groups thousands', async () => {
    const { user } = openExchanges({
      exchanges: [exchange()],
      runs: [],
      onSync: () =>
        syncTriggered(
          finishedRun({
            trigger: 'manual',
            duration_ms: 125_000,
            accounts: [accountSucceeded('bitget', { fills_seen: 5_678, fills_inserted: 1_234 })],
          }),
        ),
    });
    await venue('Bitget');

    await user.click(syncButton());

    expect(await resultBlock()).toHaveTextContent(
      'The sync succeeded: 1,234 new fills (5,678 read) from 1 exchange, in 2 minutes 5 seconds.',
    );
  });

  it('a joined run says so', async () => {
    // Pressed while a scheduled run was in flight: the request joined it, and
    // the summary is that run's.
    const scheduled = finishedRun({
      run_id: 11,
      trigger: 'scheduled',
      accounts: [
        accountSucceeded('bingx', { fills_seen: 2, fills_inserted: 2 }),
        accountSucceeded('bitget', { fills_seen: 8, fills_inserted: 0 }),
      ],
    });
    const { user } = openExchanges({
      exchanges: bothVenues({ syncing: true }),
      runs: [runningExchangeRun({ run_id: 11, accounts_total: 2 })],
      onSync: (exchanges) => {
        exchanges.setRuns([scheduled]);
        return syncTriggered(scheduled, true);
      },
    });
    await venue('Bitget');

    await user.click(syncButton());

    const result = await resultBlock();
    expect(result).toHaveTextContent(
      'The sync succeeded: 2 new fills (10 read) from 2 exchanges, in 3 seconds.',
    );
    expect(result).toHaveTextContent(JOINED_LINE);
  });

  it('a skipped account says to press Sync now again', async () => {
    // Only a joined scheduled or startup run skips an account: a sync the
    // owner starts retries it. The POST returned when that run ended, so a
    // second press starts a manual run.
    const scheduled = finishedRun({
      run_id: 11,
      trigger: 'scheduled',
      accounts: [
        accountSucceeded('bingx', { fills_seen: 4, fills_inserted: 1 }),
        accountSkipped('bitget'),
      ],
    });
    const { user } = openExchanges({
      exchanges: [
        exchange({ exchange_key: 'bingx', syncing: true }),
        authFailedExchange('auth', { syncing: true }),
      ],
      runs: [runningExchangeRun({ run_id: 11, accounts_total: 2 })],
      onSync: (exchanges) => {
        exchanges.setExchanges([exchange({ exchange_key: 'bingx' }), authFailedExchange('auth')]);
        exchanges.setRuns([scheduled]);
        return syncTriggered(scheduled, true);
      },
    });
    await venue('Bitget');

    await user.click(syncButton());

    const result = await resultBlock();
    expect(result).toHaveTextContent(JOINED_LINE);
    expect(result).toHaveTextContent(skippedLine('Bitget'));
    expect(result).not.toHaveTextContent(skippedLine('BingX'));
    // A skip is not a failure: no failure sentence for it.
    expect(result).not.toHaveTextContent(SENTENCES.bitgetAuth);
    expect(syncButton()).toBeEnabled();
  });

  it('a failed account is named with its sentence', async () => {
    const run = finishedRun({
      run_id: 9,
      trigger: 'manual',
      accounts: [
        accountFailed('bingx', 'conflict', { fills_seen: 3, fills_inserted: 0 }),
        accountSucceeded('bitget', { fills_seen: 20, fills_inserted: 20 }),
      ],
    });
    const { user } = openExchanges({
      exchanges: bothVenues(),
      runs: [],
      onSync: (exchanges) => {
        exchanges.setRuns([run]);
        return syncTriggered(run);
      },
    });
    await venue('Bitget');

    await user.click(syncButton());

    const result = await resultBlock();
    expect(result).toHaveTextContent(
      'The sync partially succeeded: 20 new fills (23 read) from 2 exchanges, in 3 seconds.',
    );
    expect(result).toHaveTextContent(`BingX: ${SENTENCES.bingxConflict}`);
    expect(result).not.toHaveTextContent(/Bitget:/);
    expect(result).not.toHaveTextContent(JOINED_LINE);
  });

  it('a run in which every account failed says the sync failed', async () => {
    const run = finishedRun({
      run_id: 9,
      trigger: 'manual',
      accounts: [accountFailed('bitget', 'unavailable')],
    });
    const { user } = openExchanges({
      exchanges: [exchange()],
      runs: [],
      onSync: () => syncTriggered(run),
    });
    await venue('Bitget');

    await user.click(syncButton());

    const result = await resultBlock();
    expect(result).toHaveTextContent(
      'The sync failed: 0 new fills (0 read) from 1 exchange, in 3 seconds.',
    );
    expect(result).toHaveTextContent(`Bitget: ${SENTENCES.bitgetUnavailable}`);
  });

  it('the result stays through polls, and a second sync replaces it', async () => {
    fakeIntervals();
    let syncs = 0;
    const { user } = openExchanges({
      exchanges: [exchange()],
      runs: [],
      onSync: () => {
        syncs += 1;
        return syncTriggered(
          finishedRun({
            run_id: syncs,
            trigger: 'manual',
            accounts: [
              accountSucceeded('bitget', { fills_seen: syncs * 10, fills_inserted: syncs }),
            ],
          }),
        );
      },
    });
    await venue('Bitget');

    await user.click(syncButton());
    expect(await resultBlock()).toHaveTextContent('1 new fill (10 read)');

    await advance(SLOW_POLL_MS);
    expect(await resultBlock()).toHaveTextContent('1 new fill (10 read)');

    await user.click(syncButton());

    await waitFor(() => {
      expect(screen.getByText(/2 new fills \(20 read\)/)).toBeInTheDocument();
    });
    expect(screen.queryByText(/1 new fill \(10 read\)/)).not.toBeInTheDocument();
    expect(await resultBlock()).toHaveTextContent('2 new fills (20 read)');
  });

  it('Sync now stays enabled while a scheduled run is syncing', async () => {
    // A status must not become a lock (spec 011). Pressing joins the run.
    const { user, fake } = openExchanges({
      exchanges: [exchange({ syncing: true })],
      runs: [runningExchangeRun({ trigger: 'scheduled', accounts_total: 1 })],
    });
    const item = await venue('Bitget');
    expect(item).toHaveTextContent('Syncing');

    expect(syncButton()).toBeEnabled();

    await user.click(syncButton());

    await waitFor(() => {
      expect(fake.requestsTo('sync')).toHaveLength(1);
    });
  });

  it('a failed sync says a run may still be going and keeps the page', async () => {
    const { user, fake } = openExchanges({
      exchanges: [exchange()],
      runs: [finishedRun()],
    });
    await venue('Bitget');
    fake.fail('sync', () =>
      problem(500, 'Internal Server Error', 'The server encountered an unexpected condition.'),
    );

    await user.click(syncButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      `${FAILURE_PREFIX} The server encountered an unexpected condition. ${MAY_STILL_RUN}`,
    );
    // The backend's detail already ends in a full stop; the template must not add another.
    expect(alert.textContent).not.toMatch(DOUBLE_PERIOD);
    // The accounts and the run log stay, and the owner can try again.
    expect(await venue('Bitget')).toHaveTextContent('Up to date');
    expect(bodyRows(await runTable())).toHaveLength(1);
    expect(syncButton()).toBeEnabled();
    expect(screen.queryByText(PENDING_LINE)).not.toBeInTheDocument();
  });

  it('a sync that never reached the server says so in words', async () => {
    const { user } = openExchanges(undefined, [
      http.post(EXCHANGE_SYNC_PATH, () => HttpResponse.error()),
    ]);
    await venue('Bitget');

    await user.click(syncButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      `${FAILURE_PREFIX} The server could not be reached. ${MAY_STILL_RUN}`,
    );
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('a sync cut off by a proxy does not quote the proxy', async () => {
    // The backend holds the request for the whole run and shields the run
    // from a dropped connection, so a proxy's 504 is the likeliest failure
    // of all, and its reason phrase is not a sentence for the owner.
    const { user } = openExchanges(undefined, [
      http.post(
        EXCHANGE_SYNC_PATH,
        () =>
          new HttpResponse('<html><body>504 Gateway Time-out</body></html>', {
            status: 504,
            statusText: 'Gateway Timeout',
            headers: { 'content-type': 'text/html' },
          }),
      ),
    ]);
    await venue('Bitget');

    await user.click(syncButton());

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      `${FAILURE_PREFIX} The server could not be reached. ${MAY_STILL_RUN}`,
    );
    expect(alert).not.toHaveTextContent(/gateway/i);
  });

  it('a failed sync still refetches the list', async () => {
    // Intervals are faked and never advanced, so no poll can be what re-reads.
    fakeIntervals();
    const { user, fake } = openExchanges({
      exchanges: [unsyncedExchange('bitget')],
      runs: [],
    });
    const item = await venue('Bitget');
    const listBefore = fake.count('list');
    const runsBefore = fake.count('runs');
    const release = fake.hold('sync');
    fake.fail('sync', () => problem(504, 'Gateway Timeout', 'The upstream did not answer.'));

    await user.click(syncButton());
    await screen.findByText(PENDING_LINE);
    // The run carries on behind the dropped request.
    fake.setExchanges([
      exchange({
        status: 'never_synced',
        syncing: true,
        last_synced_at: null,
        fills_stored: 640,
        pending_windows: 2,
      }),
    ]);
    fake.setRuns([runningExchangeRun({ trigger: 'manual', started_at: NOW })]);
    release();

    await screen.findByRole('alert');
    await waitFor(() => {
      expectFact(item, FACT.fills, '640');
    });
    expect(fake.count('list')).toBeGreaterThan(listBefore);
    expect(fake.count('runs')).toBeGreaterThan(runsBefore);
    expect(item).toHaveTextContent('Syncing');
  });

  it('a failed sync keeps polling fast while the run it left behind is still syncing', async () => {
    fakeIntervals();
    const { user, fake } = openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    await venue('Bitget');
    fake.fail('sync', () => problem(504, 'Gateway Timeout', 'The upstream did not answer.'));
    fake.setExchanges([exchange({ syncing: true, pending_windows: 4 })]);

    await user.click(syncButton());
    await screen.findByRole('alert');
    await settle();
    const reads = fake.count('list');

    await advance(FAST_POLL_MS);

    expect(fake.count('list')).toBe(reads + 1);
  });

  it('no Sync now button when no venue is configured', async () => {
    // With none configured, a sync records an empty run and does nothing else.
    openExchanges({
      exchanges: [exchange({ configured: false })],
      runs: [finishedRun({ accounts: [], status: 'success' })],
    });

    const item = await venue('Bitget');
    expect(item).toHaveTextContent(notConfiguredLine('Bitget'));

    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
  });

  it('shows Sync now when one of two venues is configured', async () => {
    openExchanges({
      exchanges: [exchange({ exchange_key: 'bingx', configured: false }), exchange()],
      runs: [],
    });
    await venue('Bitget');

    expect(syncButton()).toBeEnabled();
  });
});

/*
 * Criterion 4: the run log.
 */

describe('ExchangesPage: run log', () => {
  it('the run log shows trigger, status, duration, account and fill counts', async () => {
    const { fake } = openExchanges({
      exchanges: bothVenues(),
      runs: [
        finishedRun({
          run_id: 5,
          trigger: 'manual',
          started_at: EXCHANGE_RUN_STARTED_AT,
          duration_ms: 125_000,
          accounts: [
            accountSucceeded('bingx', { fills_seen: 30, fills_inserted: 10 }),
            accountFailed('bitget', 'unavailable', { fills_seen: 5, fills_inserted: 2 }),
          ],
        }),
        finishedRun({
          run_id: 4,
          trigger: 'scheduled',
          started_at: '2026-09-24T11:00:00.000000Z',
          duration_ms: 7_325_000,
          accounts: [
            accountSucceeded('bingx', { fills_seen: 1_500, fills_inserted: 1_200 }),
            accountSkipped('bitget'),
          ],
        }),
        finishedRun({
          run_id: 3,
          trigger: 'startup',
          started_at: '2026-09-24T10:00:00.000000Z',
          duration_ms: 20,
          accounts: [],
          status: 'success',
        }),
        interruptedExchangeRun({
          run_id: 2,
          started_at: EXCHANGE_OLD_RUN_STARTED_AT,
          accounts_total: 2,
          accounts: [accountSucceeded('bingx', { fills_seen: 9, fills_inserted: 9 })],
        }),
      ],
    });

    const table = await runTable();
    const rows = bodyRows(table);
    expect(rows).toHaveLength(4);
    const [partial, scheduled, startup, interrupted] = rows;
    if (
      partial === undefined ||
      scheduled === undefined ||
      startup === undefined ||
      interrupted === undefined
    ) {
      throw new Error('The run log is missing a row.');
    }

    // Newest first, as the API orders them.
    expect(text(cell(partial, 'Started'))).toBe('20 minutes ago');
    const started = within(cell(partial, 'Started')).getByText('20 minutes ago');
    expect(started).toHaveAttribute('dateTime', EXCHANGE_RUN_STARTED_AT);
    expect(started.getAttribute('title')).toBeTruthy();
    expect(text(cell(partial, 'Trigger'))).toBe('Manual');
    expect(text(cell(partial, 'Status'))).toBe('Partially succeeded');
    expect(text(cell(partial, 'Duration'))).toBe('2 minutes 5 seconds');
    expect(text(cell(partial, 'Exchanges'))).toBe('1 succeeded, 1 failed');
    expect(text(cell(partial, 'Fills'))).toBe('12 new of 35 read');

    expect(text(cell(scheduled, 'Started'))).toBe('1 hour ago');
    expect(text(cell(scheduled, 'Trigger'))).toBe('Scheduled');
    expect(text(cell(scheduled, 'Status'))).toBe('Succeeded');
    expect(text(cell(scheduled, 'Duration'))).toBe('2 hours 2 minutes');
    expect(text(cell(scheduled, 'Exchanges'))).toBe('1 succeeded, 1 skipped');
    expect(text(cell(scheduled, 'Fills'))).toBe('1,200 new of 1,500 read');
    expect(cell(scheduled, 'Details')).toHaveTextContent(`Bitget: ${OUTCOME_LABELS.skipped}`);
    expect(cell(scheduled, 'Details')).toHaveTextContent(`BingX: ${OUTCOME_LABELS.success}`);

    // No owner: a success with no accounts at all.
    expect(text(cell(startup, 'Trigger'))).toBe('At startup');
    expect(text(cell(startup, 'Duration'))).toBe('under a second');
    expect(text(cell(startup, 'Exchanges'))).toBe('None');
    expect(text(cell(startup, 'Fills'))).toBe('0 new of 0 read');

    // An interrupted run never finished, so it has no duration.
    expect(text(cell(interrupted, 'Status'))).toBe('Interrupted');
    expect(text(cell(interrupted, 'Duration'))).toBe('—');
    expect(text(cell(interrupted, 'Fills'))).toBe('9 new of 9 read');
    expect(cell(interrupted, 'Details')).toHaveTextContent(`BingX: ${OUTCOME_LABELS.success}`);

    // The 20 newest, and no more.
    const runReads = fake.requestsTo('runs').map((entry) => new URL(entry.url));
    expect(runReads.length).toBeGreaterThan(0);
    for (const url of runReads) {
      expect(url.searchParams.get('limit')).toBe('20');
    }
  });

  it("a failed account's error shows its sentence and detail", async () => {
    openExchanges({
      exchanges: bothVenues(),
      runs: [
        finishedRun({
          trigger: 'scheduled',
          accounts: [
            accountFailed('bingx', 'schema'),
            accountSucceeded('bitget', { fills_seen: 3, fills_inserted: 3 }),
          ],
        }),
      ],
    });

    const [row] = bodyRows(await runTable());
    if (row === undefined) {
      throw new Error('The run log has no rows.');
    }
    const details = cell(row, 'Details');

    expect(details).toHaveTextContent(`BingX: ${OUTCOME_LABELS.failed}`);
    expect(details).toHaveTextContent(SENTENCES.bingxSchema);
    expect(details).toHaveTextContent(`Detail: ${DETAILS.schema}`);
    expect(details).toHaveTextContent(`Bitget: ${OUTCOME_LABELS.success}`);
    // The succeeded account has no error to show.
    expect(details.textContent.match(/Detail:/g)).toHaveLength(1);
  });

  it('a detail holding markup renders as literal text', async () => {
    // `detail` is built by the backend from a fixed summary, a status and a
    // digits-only code, and the backend is tested for it. This side's job is
    // not to undo that guarantee: a detail is a text node, never HTML, never
    // a link.
    const markup =
      '<img src="x" data-injected="run" onerror="alert(1)"><a href="https://example.test/run">here</a>';
    const accountMarkup = markup.replaceAll('run', 'account');
    openExchanges({
      exchanges: [
        erroredExchange('invalid_request', {
          last_error: lastError('invalid_request', accountMarkup),
        }),
      ],
      runs: [
        finishedRun({
          trigger: 'scheduled',
          accounts: [accountFailed('bitget', 'invalid_request', { detail: markup })],
        }),
      ],
    });

    const [row] = bodyRows(await runTable());
    if (row === undefined) {
      throw new Error('The run log has no rows.');
    }
    const details = cell(row, 'Details');
    expect(details).toHaveTextContent(`Detail: ${markup}`);

    const item = await venue('Bitget');
    expect(item).toHaveTextContent(`Detail: ${accountMarkup}`);

    for (const container of [details, item]) {
      expect(container.querySelector('[data-injected]')).toBeNull();
      expect(container.querySelector('img')).toBeNull();
      expect(container.querySelector('a')).toBeNull();
    }
    expect(document.querySelector('a[href^="https://example.test"]')).toBeNull();
  });

  it('a failed account with no kind, from a hand-edited row, still says it failed', async () => {
    // The CHECK allows a NULL kind, and the backend reads such a row as a hand
    // edit. The page must still say the account failed, and invent nothing.
    openExchanges({
      exchanges: [exchange()],
      runs: [finishedRun({ trigger: 'scheduled', accounts: [handEditedFailure('bitget')] })],
    });

    const [row] = bodyRows(await runTable());
    if (row === undefined) {
      throw new Error('The run log has no rows.');
    }
    const details = cell(row, 'Details');

    expect(details).toHaveTextContent(`Bitget: ${OUTCOME_LABELS.failed}`);
    expect(details).not.toHaveTextContent(/detail:/i);
    expect(details).not.toHaveTextContent(/null|undefined/);
    expect(text(cell(row, 'Status'))).toBe('Failed');
  });

  it('the accounts render while the run log is still loading', async () => {
    const { fake } = openExchanges();
    const release = fake.hold('runs');

    expect(await venue('Bitget')).toHaveTextContent('Up to date');
    const section = await historySection();
    expect(within(section).getByRole('status')).toBeInTheDocument();
    expect(within(section).queryByRole('table')).not.toBeInTheDocument();
    expect(within(section).queryByText(NO_RUNS_LINE)).not.toBeInTheDocument();

    release();

    expect(bodyRows(await runTable())).toHaveLength(1);
    expect(within(section).queryByRole('status')).not.toBeInTheDocument();
  });

  it('a failed run-log poll keeps the runs on screen, with the notice', async () => {
    fakeIntervals();
    const { fake } = openExchanges();
    const table = await runTable();
    await settle();

    fake.fail('runs', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    await advance(SLOW_POLL_MS);

    const section = await historySection();
    expect(await within(section).findByRole('alert')).toHaveTextContent(
      'The database is restarting.',
    );
    expect(table).toBeInTheDocument();
    expect(bodyRows(table)).toHaveLength(1);
  });

  it('an empty run log says no sync has run', async () => {
    openExchanges({ exchanges: [exchange()], runs: [] });

    const section = await historySection();

    expect(await within(section).findByText(NO_RUNS_LINE)).toBeInTheDocument();
    expect(within(section).queryByRole('table')).not.toBeInTheDocument();
  });

  it('a failed run log is a notice and the accounts still render', async () => {
    const { fake } = openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    fake.fail('runs', () => problem(503, 'Service Unavailable', 'The database is restarting.'));

    const item = await venue('Bitget');
    expect(item).toHaveTextContent('Up to date');

    const section = await historySection();
    const notice = await within(section).findByRole('alert');
    // The backend's own sentence, as every other notice on this site gives it.
    expect(notice).toHaveTextContent('The database is restarting.');
    expect(within(section).queryByRole('table')).not.toBeInTheDocument();
    expect(within(section).queryByText(NO_RUNS_LINE)).not.toBeInTheDocument();
    // Not the whole-page failure: that is for the list alone.
    expect(
      screen.queryByRole('heading', { name: 'Could not load exchanges' }),
    ).not.toBeInTheDocument();
    expect(syncButton()).toBeEnabled();
  });

  it('the run log polls fast while its newest run is running', async () => {
    fakeIntervals();
    const { fake } = openExchanges({
      exchanges: [exchange()],
      runs: [runningExchangeRun({ accounts_total: 1 }), finishedRun()],
    });
    await runTable();
    await settle();
    const reads = fake.count('runs');

    await advance(FAST_POLL_MS - 1);
    expect(fake.count('runs')).toBe(reads);

    await advance(1);
    expect(fake.count('runs')).toBe(reads + 1);

    // The run ends; the poll that reads it drops back to one a minute.
    fake.setRuns([finishedRun({ run_id: 8, started_at: NOW }), finishedRun()]);
    await advance(FAST_POLL_MS);
    expect(fake.count('runs')).toBe(reads + 2);

    await advance(FAST_POLL_MS);
    expect(fake.count('runs')).toBe(reads + 2);

    await advance(SLOW_POLL_MS - FAST_POLL_MS);
    expect(fake.count('runs')).toBe(reads + 3);
  });
});

/*
 * Criterion 5: the truncation banner.
 */

describe('ExchangesPage: truncation banner', () => {
  it('a truncated venue gets a banner naming effective_since in UTC', async () => {
    openExchanges({ exchanges: [truncatedExchange()], runs: [finishedRun()] });
    await venue('Bitget');

    const section = banner('Bitget');

    expect(section).toHaveTextContent(bannerLine('Bitget', TRUNCATED_EFFECTIVE_SINCE_TEXT));
    expect(section).not.toHaveTextContent(/has not finished/);
    const instant = timeIn(section);
    expect(instant).toHaveTextContent(TRUNCATED_EFFECTIVE_SINCE_TEXT);
    // The exact instant, unrounded, for anything that reads the markup.
    expect(instant).toHaveAttribute('dateTime', TRUNCATED_EFFECTIVE_SINCE);
    expect(instant.getAttribute('title')).toBeTruthy();
    // Page state, not an event: a 5-second poll must not re-announce it.
    expect(section.closest('[aria-live], [role="alert"], [role="status"]')).toBeNull();
    expect(section.querySelector('[aria-live], [role="alert"], [role="status"]')).toBeNull();
    // The entry names the same instant as where its history is complete from.
    expectFact(await venue('Bitget'), FACT.history, TRUNCATED_EFFECTIVE_SINCE_TEXT);
  });

  it('the banner rounds a sub-second instant up', async () => {
    openExchanges({
      exchanges: [
        truncatedExchange({ exchange_key: 'bingx', effective_since: WHOLE_SECOND_EFFECTIVE_SINCE }),
        truncatedExchange({ effective_since: '2026-06-27T12:05:36.999000Z' }),
      ],
      runs: [],
    });
    await venue('Bitget');

    const bitget = banner('Bitget');
    expect(bitget).toHaveTextContent(bannerLine('Bitget', 'Jun 27, 2026, 12:05:37 PM UTC'));
    expect(timeIn(bitget)).toHaveAttribute('dateTime', '2026-06-27T12:05:36.999000Z');
    // A whole second has nothing to round.
    expect(banner('BingX')).toHaveTextContent(
      bannerLine('BingX', WHOLE_SECOND_EFFECTIVE_SINCE_TEXT),
    );
  });

  it('the banner says the import has not finished while windows are pending', async () => {
    openExchanges({ exchanges: [truncatedExchange({ pending_windows: 4 })], runs: [] });
    await venue('Bitget');

    const section = banner('Bitget');

    expect(section).toHaveTextContent(bannerLine('Bitget', TRUNCATED_EFFECTIVE_SINCE_TEXT));
    expect(section).toHaveTextContent(bannerPendingLine(4));
  });

  it('no banner when history is not truncated', async () => {
    openExchanges({
      exchanges: [
        // The whole request is held.
        exchange({ exchange_key: 'bingx' }),
        // Nothing planned yet: truncation is unknown, and unknown is not truncated.
        unsyncedExchange('bitget'),
      ],
      runs: [],
    });
    await venue('Bitget');

    expect(
      screen.queryByRole('heading', { name: /history is incomplete/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/retention window/)).not.toBeInTheDocument();
  });

  it('one banner for each truncated venue, and none for the other', async () => {
    openExchanges({
      exchanges: [
        truncatedExchange({ exchange_key: 'bingx' }),
        exchange({ requested_since: RECENT_REQUESTED_SINCE }),
      ],
      runs: [],
    });
    await venue('Bitget');

    expect(screen.getAllByRole('heading', { name: /history is incomplete/ })).toHaveLength(1);
    expect(banner('BingX')).toHaveTextContent(bannerLine('BingX', TRUNCATED_EFFECTIVE_SINCE_TEXT));
  });

  it('a banner for each of two truncated venues', async () => {
    openExchanges({
      exchanges: [
        truncatedExchange({ exchange_key: 'bingx' }),
        truncatedExchange({ requested_since: OLD_REQUESTED_SINCE }),
      ],
      runs: [],
    });
    await venue('Bitget');

    expect(screen.getAllByRole('heading', { name: /history is incomplete/ })).toHaveLength(2);
  });

  it('the banner comes before the accounts in document order', async () => {
    openExchanges({ exchanges: [truncatedExchange()], runs: [] });
    const accounts = await accountsSection();

    const section = banner('Bitget');

    expect(
      section.compareDocumentPosition(accounts) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // And below the toolbar.
    expect(
      syncButton().compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(section.contains(accounts)).toBe(false);
  });
});

/*
 * Criteria 6 and 7: the empty state, loading and failure.
 */

describe('ExchangesPage: empty, loading and failure', () => {
  it('no exchange: the empty state says keys come from environment variables on the host', async () => {
    openExchanges({ exchanges: [], runs: [] });

    const heading = await screen.findByRole('heading', { name: EMPTY_TITLE });
    const main = screen.getByRole('main');

    expect(main).toHaveTextContent(EMPTY_TEXT);
    expect(heading).toBeInTheDocument();
    // No Sync now: there is nothing to sync. No run log either.
    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Sync history' })).not.toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
    // And nothing to type a key into.
    expect(main.querySelector('input, textarea, select, [contenteditable]')).toBeNull();
    // An empty state, not a failure.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('the page is headed Exchanges, with an Accounts and a Sync history section', async () => {
    openExchanges();

    expect(await screen.findByRole('heading', { level: 2, name: 'Exchanges' })).toBeInTheDocument();
    expect(await accountsSection()).toBeInTheDocument();
    expect(await historySection()).toBeInTheDocument();
    expect(currentPath()).toBe('/exchanges');
  });

  it('shows a skeleton while loading', async () => {
    const { fake } = openExchanges();
    const release = fake.hold('list');

    const skeleton = await screen.findByText('Loading exchanges…');
    expect(skeleton.closest('[role="status"]')).not.toBeNull();
    await waitFor(() => {
      expect(fake.count('list')).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('heading', { name: 'Accounts' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();

    release();

    expect(await venue('Bitget')).toHaveTextContent('Up to date');
    expect(screen.queryByText('Loading exchanges…')).not.toBeInTheDocument();
  });

  it('a failed first load is a page error with retry', async () => {
    const { user, fake } = openExchanges();
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));

    const alert = await screen.findByRole('alert');
    expect(
      within(alert).getByRole('heading', { name: 'Could not load exchanges' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The database is restarting.');
    expect(screen.queryByRole('heading', { name: 'Accounts' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
    // Nothing loaded is not the same as nothing configured.
    expect(screen.queryByRole('heading', { name: EMPTY_TITLE })).not.toBeInTheDocument();

    fake.fail('list', null);
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await venue('Bitget')).toHaveTextContent('Up to date');
    expect(
      screen.queryByRole('heading', { name: 'Could not load exchanges' }),
    ).not.toBeInTheDocument();
  });

  it('a first load that never reached the server says so in words', async () => {
    openExchanges(undefined, [http.get(EXCHANGES_PATH, () => HttpResponse.error())]);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('heading', { name: 'Could not load exchanges' })).toBeTruthy();
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('a failed poll keeps the entries on screen', async () => {
    fakeIntervals();
    const { fake } = openExchanges();
    const item = await venue('Bitget');
    await settle();

    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    await advance(SLOW_POLL_MS);

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(/could not/i);
    // The entries already on screen stay, and so does everything else.
    expect(item).toBeInTheDocument();
    expect(await venue('Bitget')).toHaveTextContent('Up to date');
    expect(
      screen.queryByRole('heading', { name: 'Could not load exchanges' }),
    ).not.toBeInTheDocument();
    expect(syncButton()).toBeEnabled();
    expect(await runTable()).toBeInTheDocument();

    // The next good poll clears the notice.
    fake.fail('list', null);
    await advance(SLOW_POLL_MS);

    await waitFor(() => {
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });

  it('a 401 from the exchange read returns to the login page', async () => {
    // The session died between the session read and the page's own.
    const session = fakeSession({ initialUser: TEST_USERNAME });
    const fake = fakeExchanges({ exchanges: [exchange()], runs: [], session });
    server.use(...session.handlers, ...fake.handlers);
    session.signOut();
    server.use(http.get('/api/auth/session', () => HttpResponse.json({ username: TEST_USERNAME })));

    renderApp(['/exchanges']);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
  });
});

/*
 * Polling: each query decides its own rate from its own data.
 */

describe('ExchangesPage: polling', () => {
  it('the list polls fast while a venue is syncing and slows when it stops', async () => {
    fakeIntervals();
    const { fake } = openExchanges({
      exchanges: [exchange({ syncing: true })],
      runs: [finishedRun()],
    });
    const item = await venue('Bitget');
    await settle();
    const reads = fake.count('list');

    await advance(FAST_POLL_MS - 1);
    expect(fake.count('list')).toBe(reads);

    await advance(1);
    expect(fake.count('list')).toBe(reads + 1);

    // The run ends. The next fast poll reads that, and the rate drops.
    fake.patchExchange('bitget', { syncing: false });
    await advance(FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads + 2);
    await waitFor(() => {
      expect(item).toHaveTextContent('Up to date');
    });

    await advance(FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads + 2);

    await advance(SLOW_POLL_MS - FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads + 3);
  });

  it('a scheduled run that starts while the page is open is noticed within a minute', async () => {
    fakeIntervals();
    const { fake } = openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    const item = await venue('Bitget');
    await settle();
    const reads = fake.count('list');

    fake.patchExchange('bitget', { syncing: true });
    await advance(FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads);

    await advance(SLOW_POLL_MS - FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads + 1);
    await waitFor(() => {
      expect(item).toHaveTextContent('Syncing');
    });

    // From then on it is polled fast.
    await advance(FAST_POLL_MS);
    expect(fake.count('list')).toBe(reads + 2);
  });

  it("both queries poll fast while this page's own sync is pending", async () => {
    fakeIntervals();
    const { user, fake } = openExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    await venue('Bitget');
    await runTable();
    const release = fake.hold('sync');

    await user.click(syncButton());
    await screen.findByText(PENDING_LINE);
    await settle();
    const list = fake.count('list');
    const runs = fake.count('runs');

    await advance(FAST_POLL_MS);

    expect(fake.count('list')).toBe(list + 1);
    expect(fake.count('runs')).toBe(runs + 1);

    release();
  });
});
