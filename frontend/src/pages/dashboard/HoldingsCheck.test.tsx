import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  ALL_RECOMPUTE_OUTCOMES,
  bgbFeeNeverHeld,
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
  feeInNeverHeldAsset,
  investedPortfolio,
  kasLossPortfolio,
  lastRecompute,
  noSnapshot,
  position,
  positionsResponse,
  RECOMPUTE_FAILED_AT,
  stablecoinOnlySnapshot,
  totals,
  xrpClosed,
  ZERO,
  type PositionsInput,
  type PositionsResponse,
} from '@/test/accountingFixtures';
import { firstTrade, firstTrades } from '@/test/adjustmentFixtures';
import {
  accountSucceeded,
  erroredExchange,
  exchange,
  finishedRun,
  syncTriggered,
  type ExchangeResponse,
} from '@/test/exchangeFixtures';
import {
  fakeAccounting,
  POSITIONS_PATH,
  RECONCILIATION_PATH,
  type FakeAccounting,
} from '@/test/fakeAccounting';
import { fakeAdjustments } from '@/test/fakeAdjustments';
import { fakeExchanges, type FakeExchanges, type FakeExchangesOptions } from '@/test/fakeExchanges';
import { fakePortfolio, type FakePortfolioOptions } from '@/test/fakePortfolio';
import { emptyPortfolio, healthyPortfolio, NOW } from '@/test/fixtures';
import {
  assetReconciliation,
  BALANCES_READ_AT,
  BTC_BELOW,
  BTC_BEYOND,
  btcBelowPosition,
  btcBeyondPosition,
  EDGE_BALANCES_READ_AT,
  ETH_BEYOND,
  exchangeBalances,
  failedBalances,
  failedChain,
  investedPortfolioGaps,
  kasNeverTraded,
  matchedAsset,
  notReconciled,
  OLD_BALANCES_READ_AT,
  OTHER_BALANCES_READ_AT,
  outOfDateBalances,
  reconciliation,
  SOL_OVER,
  solOver,
  syncFailedBalances,
  unreadBalances,
  walletReadings,
  WALLETS_OBSERVED_AT,
  XRP_LEFT,
  xrpLeftOnExchange,
  type ExchangeBalancesResponse,
  type FailedChainResponse,
  type ReconciliationResponse,
} from '@/test/reconciliationFixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The holdings check of the dashboard (spec 025, acceptance criterion 8), rendered inside the
 * whole app at `/`: the block under the invested section, and the badge it puts on a position.
 *
 * Every response is a pair - a snapshot and the comparison of that same snapshot - which
 * `fakeAccounting` refuses to serve when the two disagree, and every comparison's figures are
 * worked by hand in `test/reconciliationFixtures.ts`. Each `<data value>` is compared with the
 * exact wire string, 18 places and trailing zeros included.
 *
 * `Date` is faked and fixed at `NOW`; `setTimeout` stays real, because MSW answers through it.
 * A test that needs a poll fakes `setInterval` too.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

/*
 * The sentences, written out: each is a statement to the owner about what they hold, and a
 * change to one should be a diff here.
 */
const COMPARISON =
  'Compares what the history says is held with the balances read from your wallets and from ' +
  "your exchanges' spot accounts. A difference of 1% or less counts as a match.";
const GUIDANCE =
  'The balances read hold more than the history accounts for. The usual cause is buys older ' +
  'than an exchange keeps, or coins acquired elsewhere, and average cost and profit then leave ' +
  'those units out. Before recording anything, rule out coins in transit: readings are taken ' +
  'at different moments, so coins moved between two of them are counted twice until both ' +
  'have been read again. How old each reading is, is shown below the lists.';
/** Spec 027: the line after the guidance, followed by one link per asset listed. */
const RECORD_PROMPT = 'If the gap is real, record the missing coins for:';
const OVER_EXPLANATION =
  'The history accounts for more than the balances read. The causes include coins held where ' +
  'this application does not read them (another wallet, an Earn product, or a futures or ' +
  'funding account), withdrawals, network fees and trading fees the import did not record, ' +
  'and a sale or conversion the import did not see. This check cannot tell them apart, so it ' +
  'flags nothing.';
const DIFFERENCE_LEGEND =
  'Difference is the wallets plus the exchanges, minus what the history accounts for.';
const ALL_MATCH = 'Every quantity matches the balances read.';
const NOTHING_TO_COMPARE = 'There is nothing to compare yet.';
const BADGE = 'Held exceeds history';
const BADGE_EXPLANATION =
  'The balances read hold more of this asset than the history accounts for. The usual cause ' +
  'is a buy the history does not show, which leaves those units out of its average cost and ' +
  'profit; coins in transit between two readings can look the same. The holdings check below ' +
  'says more.';
const LOAD_ERROR_TITLE = 'Could not load the holdings check';
/**
 * `.+` is the instant, which a test under a named zone compares on its own. "May be" (R10): a
 * recompute can fail with nothing new to replay, and the alert does not claim otherwise.
 */
const STALE_HISTORY =
  /^The last recompute of the history failed on .+, so the history may be older than the balances and nothing is compared\.$/;

const COLUMNS = ['Asset', 'In history', 'Wallets', 'Exchanges', 'Difference'];

/** BTC alone, fully comparable: `position()`, whose totals are its own figures. */
function btcOnly(overrides: PositionsInput = {}): PositionsResponse {
  return positionsResponse({
    ...overrides,
    positions: [position()],
    totals: totals({
      total_invested: '52500.000000000000000000',
      market_value: '90000.000000000000000000',
      unrealized_pnl: '37500.000000000000000000',
      unrealized_return_pct: '71.4286',
      realized_pnl: '7500.000000000000000000',
    }),
  });
}

/** BTC held beyond its 1.5: wallets 1.62345678, exchanges 0.25, difference +0.37345678. */
function btcShort(overrides: Partial<ReconciliationResponse> = {}): ReconciliationResponse {
  return reconciliation({
    assets: [btcBeyondPosition()],
    wallets: walletReadings(2),
    ...overrides,
  });
}

/** BTC read below its 1.5: exchanges 1.0, difference -0.5. */
function btcOver(overrides: Partial<ReconciliationResponse> = {}): ReconciliationResponse {
  return reconciliation({ assets: [btcBelowPosition()], ...overrides });
}

/** The two venues the full comparison names, both synced: the list it is coherent with. */
function bothVenues(): ExchangeResponse[] {
  return [exchange({ exchange_key: 'bingx' }), exchange()];
}

interface OpenOptions {
  /** The snapshot. Defaults to {@link btcOnly}. */
  readonly positions?: PositionsResponse;
  /** The comparison of that snapshot. Defaults to the quiet one: everything matches. */
  readonly reconciliation?: ReconciliationResponse;
  /** The exchange list. Defaults to one venue that has imported trades and is fine. */
  readonly exchanges?: readonly ExchangeResponse[];
  readonly portfolio?: FakePortfolioOptions;
  /** Run before the first render, so a fake can be held or failed from the start. */
  readonly before?: (fakes: { accounting: FakeAccounting; exchanges: FakeExchanges }) => void;
  readonly overrides?: readonly HttpHandler[];
  readonly onExchangeSync?: FakeExchangesOptions['onSync'];
}

interface Setup {
  readonly user: UserEvent;
  readonly accounting: FakeAccounting;
  readonly exchanges: FakeExchanges;
}

function openDashboard(options: OpenOptions = {}): Setup {
  const user = userEvent.setup();
  const accounting = fakeAccounting({
    positions: options.positions ?? btcOnly(),
    ...(options.reconciliation === undefined ? {} : { reconciliation: options.reconciliation }),
  });
  const exchanges = fakeExchanges({
    exchanges: options.exchanges ?? [exchange()],
    ...(options.onExchangeSync === undefined ? {} : { onSync: options.onExchangeSync }),
  });
  options.before?.({ accounting, exchanges });
  server.use(
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fakePortfolio(options.portfolio ?? healthyPortfolio()).handlers,
    ...accounting.handlers,
    ...exchanges.handlers,
  );
  server.use(...(options.overrides ?? []));

  renderApp(['/details']);

  return { user, accounting, exchanges };
}

async function investedRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Invested' });
}

/** The block, once it is on screen: a region named by its own heading. */
async function holdingsCheck(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Holdings check' });
}

function queryHoldingsCheck(): HTMLElement | null {
  return screen.queryByRole('region', { name: 'Holdings check' });
}

/**
 * Waits until the reconciliation has been asked for and every effect of its answer has run,
 * for the tests whose expectation is that nothing was rendered: an absence asserted before the
 * answer landed would pass for the wrong reason.
 */
async function reconciliationRead(accounting: FakeAccounting): Promise<void> {
  await waitFor(() => {
    expect(accounting.count('reconciliation')).toBeGreaterThan(0);
  });
  await settle();
}

/** The positions table: the one inside the "Per asset" scroll region, not the block's. */
async function positionsTable(): Promise<HTMLElement> {
  const scroller = await within(await investedRegion()).findByRole('region', {
    name: 'Per asset',
  });
  return within(scroller).getByRole('table');
}

/** The row header of `symbol` in the positions table, badges or not. */
async function positionHeader(symbol: string): Promise<HTMLElement> {
  return within(await positionsTable()).getByRole('rowheader', {
    name: new RegExp(`^${symbol}(\\s|$)`),
  });
}

/** The exact strings every `<data value>` inside `element` carries. */
function dataValues(element: HTMLElement): (string | null)[] {
  return Array.from(element.querySelectorAll('data')).map((data) => data.getAttribute('value'));
}

interface RenderedRow {
  readonly asset: string;
  /** The visible text of each quantity cell, in column order. */
  readonly text: string[];
  /** The `<data value>` of each quantity cell, in column order. */
  readonly values: (string | null)[];
}

/** The header texts and the body rows of one of the block's tables. */
function readTable(table: HTMLElement): { columns: string[]; rows: RenderedRow[] } {
  const columns = within(table)
    .getAllByRole('columnheader')
    .map((header) => header.textContent.trim());
  const rows = within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) => {
      const cells = within(row).getAllByRole('cell');
      return {
        asset: within(row).getByRole('rowheader').textContent,
        text: cells.map((cell) => cell.textContent.trim()),
        values: cells.map((cell) => {
          const data = cell.querySelectorAll('data');
          if (data.length !== 1) {
            throw new Error(`A quantity cell holds ${String(data.length)} <data> elements.`);
          }
          return data[0]?.getAttribute('value') ?? null;
        }),
      };
    });
  return { columns, rows };
}

/** The table of the assets held beyond their history, found by the heading that names it. */
function shortTable(block: HTMLElement, count: number): HTMLElement {
  return within(block).getByRole('table', { name: `Held exceeds history (${String(count)})` });
}

/** The line that offers the way out: the paragraph the prompt starts. */
function recordLine(block: HTMLElement): HTMLElement {
  return within(block).getByText((_text, element) => {
    return element?.tagName === 'P' && element.textContent.startsWith(RECORD_PROMPT);
  });
}

/** Every link inside `element`: its text and where it goes. */
function linksIn(element: HTMLElement): { text: string; href: string | null }[] {
  return within(element)
    .queryAllByRole('link')
    .map((link) => ({ text: link.textContent, href: link.getAttribute('href') }));
}

/** The disclosure of the assets whose history is above the balances read. */
function overDisclosure(block: HTMLElement): HTMLDetailsElement {
  const details = block.querySelector('details');
  if (details === null) {
    throw new Error('The block has no disclosure.');
  }
  return details;
}

/** The "Balances last read" list: each line's text and the instant its `<time>` carries. */
function readings(block: HTMLElement): { text: string; at: string | null }[] {
  return within(within(block).getByRole('list'))
    .getAllByRole('listitem')
    .map((item) => ({
      text: item.textContent,
      at: item.querySelector('time')?.getAttribute('datetime') ?? null,
    }));
}

/** The marker legend under the positions: each badge text, in order, and its explanation. */
function legend(region: HTMLElement): { badge: string; explanation: string }[] {
  const known = ['History incomplete', 'Fee not valued', 'Unknown cost', BADGE];
  return within(region)
    .queryAllByRole('term')
    .filter((dt) => known.includes(dt.textContent.trim()))
    .map((dt) => ({
      badge: dt.textContent.trim(),
      explanation: dt.nextElementSibling?.textContent.trim() ?? '',
    }));
}

/** Every string anywhere in a JSON document, for "each `<data value>` is a wire string". */
function wireStrings(document: unknown): Set<string> {
  const found = new Set<string>();
  const visit = (value: unknown): void => {
    if (typeof value === 'string') {
      found.add(value);
    } else if (Array.isArray(value)) {
      value.forEach(visit);
    } else if (value !== null && typeof value === 'object') {
      Object.values(value).forEach(visit);
    }
  };
  visit(document);
  return found;
}

/** Fakes the poll's interval as well as the clock, for a test that advances a minute. */
function fakePolling(): void {
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
  vi.setSystemTime(new Date(NOW));
}

function nextPoll(): void {
  act(() => {
    vi.advanceTimersByTime(60_000);
  });
}

describe('HoldingsCheck: the query states (criterion 8)', () => {
  it('pending: renders nothing, and adds no loading region to the one the section shows', async () => {
    let release: () => void = () => undefined;
    const { accounting } = openDashboard({
      reconciliation: btcShort(),
      before: (fakes) => {
        release = fakes.accounting.hold('reconciliation');
      },
    });

    // The positions are on screen, and the reconciliation has been asked for and not answered.
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    await reconciliationRead(accounting);
    const invested = await investedRegion();
    expect(queryHoldingsCheck()).toBeNull();
    expect(within(invested).queryByRole('heading', { name: 'Holdings check' })).toBeNull();
    expect(within(invested).queryByRole('status')).toBeNull();
    expect(within(invested).queryByRole('alert')).toBeNull();
    expect(invested).not.toHaveTextContent(/balances read|Balances last read/);

    release();

    const block = await holdingsCheck();
    expect(invested).toContainElement(block);
    expect(shortTable(block, 1)).toBeInTheDocument();
  });

  it('pending beside pending positions: one loading region, the section own', async () => {
    const { accounting } = openDashboard({
      before: (fakes) => {
        fakes.accounting.hold('positions');
        fakes.accounting.hold('reconciliation');
      },
    });

    const invested = await investedRegion();
    const status = await within(invested).findByRole('status');
    await reconciliationRead(accounting);
    expect(within(invested).getAllByRole('status')).toEqual([status]);
    expect(status).toHaveTextContent('Loading invested per asset…');
    expect(queryHoldingsCheck()).toBeNull();
  });

  it('failed with no data: an error of its own at heading level 3, and Retry refetches', async () => {
    const { user, accounting } = openDashboard({
      reconciliation: btcShort(),
      before: (fakes) => {
        fakes.accounting.fail(
          () => problem(503, 'Service Unavailable', 'The balances table is locked.'),
          'reconciliation',
        );
      },
    });

    const invested = await investedRegion();
    const alert = await within(invested).findByRole('alert');
    expect(within(alert).getByRole('heading', { level: 3 })).toHaveTextContent(LOAD_ERROR_TITLE);
    expect(alert).toHaveTextContent('The balances table is locked.');
    // A failure is not "nothing to compare", not a match and not a finding.
    expect(queryHoldingsCheck()).toBeNull();
    expect(invested).not.toHaveTextContent(ALL_MATCH);
    expect(invested).not.toHaveTextContent(NOTHING_TO_COMPARE);
    expect(invested).not.toHaveTextContent(/Held exceeds history/);
    // It fails on its own: the positions beside it are untouched.
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(dataValues(await positionsTable())).toContain('52500.000000000000000000');
    // Counted once nothing is in flight: the positions table reads the same query, and its
    // mounting on a failed read asks again, which fails the same way.
    await settle();
    const before = accounting.count('reconciliation');
    expect(within(invested).getByRole('alert')).toBe(alert);

    accounting.fail(null, 'reconciliation');
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    const block = await holdingsCheck();
    // The button asked again, once, and that answer is what is on screen.
    expect(accounting.count('reconciliation')).toBe(before + 1);
    expect(shortTable(block, 1)).toBeInTheDocument();
    expect(within(invested).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(invested).queryByRole('heading', { name: LOAD_ERROR_TITLE })).toBeNull();
  });

  it('failed with no data: says the backend could not be reached when nothing arrives', async () => {
    openDashboard({ overrides: [http.get(RECONCILIATION_PATH, () => HttpResponse.error())] });

    const alert = await within(await investedRegion()).findByRole('alert');
    expect(within(alert).getByRole('heading', { level: 3 })).toHaveTextContent(LOAD_ERROR_TITLE);
    expect(alert).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('failed with no data: treats a truncated response as its error, not a crash', async () => {
    openDashboard({
      overrides: [
        http.get(
          RECONCILIATION_PATH,
          () =>
            new HttpResponse('{"computed_at":"2026-09-24T11:45:00Z","assets":[{"asset":"BT', {
              status: 200,
              headers: { 'content-type': 'application/json' },
            }),
        ),
      ],
    });

    const invested = await investedRegion();
    const alert = await within(invested).findByRole('alert');
    expect(within(alert).getByRole('heading')).toHaveTextContent(LOAD_ERROR_TITLE);
    expect(alert).not.toHaveTextContent(/unexpected (end|token)|syntaxerror/i);
    // The page is still standing.
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
  });

  it('failed with no data: the positions failing too leaves two errors, each with its own retry', async () => {
    const { user, accounting } = openDashboard({
      before: (fakes) => {
        fakes.accounting.fail(() => problem(503, 'Service Unavailable', 'Positions are locked.'));
        fakes.accounting.fail(
          () => problem(503, 'Service Unavailable', 'Balances are locked.'),
          'reconciliation',
        );
      },
    });

    const invested = await investedRegion();
    await waitFor(() => {
      expect(within(invested).getAllByRole('alert')).toHaveLength(2);
    });
    const [positionsAlert, checkAlert] = within(invested).getAllByRole('alert');
    expect(positionsAlert).toHaveTextContent('Could not load invested per asset');
    expect(checkAlert).toHaveTextContent(LOAD_ERROR_TITLE);
    expect(checkAlert).toHaveTextContent('Balances are locked.');

    // Retrying the holdings check asks for the holdings check, and not for the positions.
    const positionsBefore = accounting.count('positions');
    accounting.fail(null, 'reconciliation');
    await user.click(within(checkAlert ?? invested).getByRole('button', { name: 'Try again' }));

    await holdingsCheck();
    expect(accounting.count('positions')).toBe(positionsBefore);
    expect(within(invested).getAllByRole('alert')).toEqual([positionsAlert]);
  });

  it('no snapshot: renders nothing, even with every source missing', async () => {
    const { accounting } = openDashboard({
      positions: noSnapshot(),
      reconciliation: notReconciled({
        exchanges: [failedBalances('bingx', 'auth', null), unreadBalances('bitget')],
        wallets: walletReadings(1, { unread: 2 }),
      }),
    });

    const invested = await investedRegion();
    await within(invested).findByRole('heading', { name: 'Positions have not been computed yet' });
    await reconciliationRead(accounting);
    // "Not computed" is said once, by the section - and never as "every balance is unaccounted for".
    expect(queryHoldingsCheck()).toBeNull();
    expect(within(invested).queryByRole('heading', { name: 'Holdings check' })).toBeNull();
    expect(within(invested).queryByRole('alert')).toBeNull();
    expect(invested).not.toHaveTextContent(/balances|wallets? ha(s|ve) not been read|compare/i);
    expect(invested.querySelector('data')).toBeNull();
  });

  it('no snapshot: still nothing when a later refresh fails', async () => {
    fakePolling();
    const { accounting } = openDashboard({ positions: noSnapshot() });
    const invested = await investedRegion();
    await within(invested).findByRole('heading', { name: 'Positions have not been computed yet' });
    await reconciliationRead(accounting);

    accounting.fail(
      () => problem(503, 'Service Unavailable', 'Try again shortly.'),
      'reconciliation',
    );
    nextPoll();
    await waitFor(() => {
      expect(accounting.count('reconciliation')).toBe(2);
    });
    await settle();

    expect(queryHoldingsCheck()).toBeNull();
    expect(within(invested).queryByRole('alert')).toBeNull();
    expect(invested).not.toHaveTextContent(/holdings check/i);
  });

  it('loaded: the block, under its own heading, after the positions', async () => {
    openDashboard({ reconciliation: btcShort() });

    const block = await holdingsCheck();
    const invested = await investedRegion();
    expect(invested).toContainElement(block);
    const heading = within(block).getByRole('heading', { level: 3, name: 'Holdings check' });
    expect(heading).toHaveAttribute('id', 'holdings-check');
    expect(within(block).getByText(COMPARISON)).toBeInTheDocument();
    // It renders after the section's own content.
    const positions = await positionsTable();
    expect(
      positions.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // No error and no notice: nothing is wrong with the reading itself.
    expect(within(invested).queryByRole('alert')).toBeNull();
    expect(within(invested).queryByRole('heading', { name: LOAD_ERROR_TITLE })).toBeNull();
  });

  it('a failed refresh keeps the block, says why beside it, and the next good one clears it', async () => {
    fakePolling();
    const { accounting } = openDashboard({ reconciliation: btcShort() });
    const block = await holdingsCheck();
    expect(readTable(shortTable(block, 1)).rows[0]?.values).toEqual([
      BTC_BEYOND.history,
      BTC_BEYOND.wallets,
      BTC_BEYOND.exchanges,
      BTC_BEYOND.difference,
    ]);

    accounting.fail(
      () => problem(503, 'Service Unavailable', 'The database is restarting.'),
      'reconciliation',
    );
    nextPoll();

    const notice = await within(block).findByRole('alert');
    expect(notice.textContent).toBe(
      'Could not refresh the holdings check: The database is restarting. ' +
        'Showing what was last loaded.',
    );
    // What was last loaded is still there, figure for figure, and so is the badge drawn from it.
    expect(readTable(shortTable(block, 1)).rows[0]?.values).toEqual([
      BTC_BEYOND.history,
      BTC_BEYOND.wallets,
      BTC_BEYOND.exchanges,
      BTC_BEYOND.difference,
    ]);
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    const invested = await investedRegion();
    expect(within(invested).queryByRole('heading', { name: LOAD_ERROR_TITLE })).toBeNull();
    expect(within(invested).getAllByRole('alert')).toEqual([notice]);

    accounting.fail(null, 'reconciliation');
    nextPoll();

    await waitFor(() => {
      expect(within(invested).queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(shortTable(await holdingsCheck(), 1)).toBeInTheDocument();
  });

  it('a failed refresh that never arrives says the server could not be reached', async () => {
    fakePolling();
    const { accounting } = openDashboard();
    const block = await holdingsCheck();

    accounting.fail(() => HttpResponse.error(), 'reconciliation');
    nextPoll();

    expect((await within(block).findByRole('alert')).textContent).toBe(
      'Could not refresh the holdings check: The server could not be reached. ' +
        'Showing what was last loaded.',
    );
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
  });

  it('survives the positions failing: the block stays beside the section error', async () => {
    openDashboard({
      reconciliation: btcShort(),
      before: (fakes) => {
        fakes.accounting.fail(() => problem(503, 'Service Unavailable', 'Positions are locked.'));
      },
    });

    const invested = await investedRegion();
    const block = await holdingsCheck();
    expect(shortTable(block, 1)).toBeInTheDocument();
    const alert = await within(invested).findByRole('alert');
    expect(alert).toHaveTextContent('Could not load invested per asset');
    expect(block).not.toContainElement(alert);
  });

  it('picks up a new reading on its next poll, without a reload', async () => {
    fakePolling();
    const { accounting } = openDashboard();
    const block = await holdingsCheck();
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();

    // A balance read on the server: same snapshot, new comparison.
    accounting.setReconciliation(btcShort());
    nextPoll();

    await waitFor(() => {
      expect(within(block).queryByText(ALL_MATCH)).not.toBeInTheDocument();
    });
    expect(shortTable(block, 1)).toBeInTheDocument();
  });

  it('appears once the first snapshot is written', async () => {
    fakePolling();
    const { accounting } = openDashboard({ positions: noSnapshot() });
    await reconciliationRead(accounting);
    expect(queryHoldingsCheck()).toBeNull();

    accounting.setPositions(btcOnly(), btcShort());
    nextPoll();

    expect(shortTable(await holdingsCheck(), 1)).toBeInTheDocument();
  });

  it('a 401 on the reconciliation read returns to the login page', async () => {
    openDashboard({
      overrides: [
        http.get(RECONCILIATION_PATH, () =>
          problem(401, 'Unauthorized', 'Authentication is required.'),
        ),
      ],
    });

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
  });

  it('ignores fields the backend adds later', async () => {
    const response = btcShort();
    openDashboard({
      overrides: [
        http.get(RECONCILIATION_PATH, () =>
          HttpResponse.json({
            ...response,
            ...{ schema_version: 2 },
            assets: response.assets.map((entry) => ({ ...entry, ...{ value_usd: '1.00' } })),
          }),
        ),
      ],
    });

    const block = await holdingsCheck();
    expect(readTable(shortTable(block, 1)).rows[0]?.values).toEqual([
      BTC_BEYOND.history,
      BTC_BEYOND.wallets,
      BTC_BEYOND.exchanges,
      BTC_BEYOND.difference,
    ]);
    expect(block).not.toHaveTextContent(/schema_version|value_usd/);
  });
});

describe('HoldingsCheck: what is compared', () => {
  it('says what is compared, and that a difference of the tolerance or less is a match', async () => {
    openDashboard();

    const block = await holdingsCheck();
    expect(within(block).getByText(COMPARISON)).toBeInTheDocument();
    // "or less": the rule is `<=`, so a difference of exactly 1% is a match.
    expect(block).not.toHaveTextContent(/under 1%/);
  });

  it('takes the percentage from the response, not from a copy of the rule', async () => {
    // At 2.5 percent BTC's +0.37345678 on 1.87345678 is still far outside: the list stands.
    openDashboard({ reconciliation: btcShort({ tolerance_pct: '2.5' }) });

    const block = await holdingsCheck();
    expect(block).toHaveTextContent('A difference of 2.5% or less counts as a match.');
    expect(block).not.toHaveTextContent('of 1% or less');
  });

  it('puts the tolerance in the sentence as text: it is not an amount of the owner', async () => {
    openDashboard();

    const block = await holdingsCheck();
    expect(within(block).getByText(COMPARISON).querySelector('data')).toBeNull();
  });
});

/**
 * BTC's 1.5 sits on BingX, read and compared, so the asset matches whatever Bitget's state is:
 * the notice under test is the only thing a venue left out adds to the block.
 */
function btcOnBingx(bitget: ExchangeBalancesResponse): ReconciliationResponse {
  return reconciliation({
    assets: [matchedAsset('BTC', '1.500000000000000000')],
    exchanges: [
      exchangeBalances({ exchange_key: 'bingx', balances_read_at: OTHER_BALANCES_READ_AT }),
      bitget,
    ],
  });
}

/**
 * BTC held in wallets alone, beyond its 1.5: wallets 1.62345678, no exchange, difference
 * 1.62345678 - 1.5 = +0.12345678; 12.345678 against 1.62345678, so `history_short`. It needs
 * no venue, which lets every venue beside it be left out.
 */
function btcShortInWallets(overrides: Partial<ReconciliationResponse>): ReconciliationResponse {
  return reconciliation({
    assets: [
      assetReconciliation({
        history_quantity: '1.500000000000000000',
        wallet_quantity: '1.623456780000000000',
        exchange_quantity: ZERO,
        held_quantity: '1.623456780000000000',
        difference: '0.123456780000000000',
      }),
    ],
    wallets: walletReadings(2),
    ...overrides,
  });
}

describe('HoldingsCheck: the sources left out of the comparison (R9)', () => {
  it('raises no alert when every venue is compared and no wallet is stale or unread', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    expect(within(block).queryByRole('alert')).toBeNull();
    expect(within(await investedRegion()).queryByRole('alert')).toBeNull();
  });

  it('read_failed with a previous reading: why, that the reading is not used, and its instant', async () => {
    inTimeZone('UTC');
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcOnBingx(failedBalances('bitget', 'unavailable')),
    });

    const block = await holdingsCheck();
    const alert = within(block).getByRole('alert');
    const time = alert.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe(OLD_BALANCES_READ_AT);
    expect(time?.textContent).toMatch(/^Sep 21, 2026, 12:00\sPM$/);
    expect(alert.textContent).toBe(
      'The balances at Bitget could not be read. Bitget could not be reached, or answered ' +
        `that it was unavailable. Its last good reading, from ${time?.textContent ?? ''}, is ` +
        'not used, so the coins held there are left out of the comparison.',
    );
    // An instant that does not tick: a relative phrase re-announces itself in a live region.
    expect(alert).not.toHaveTextContent(/ago|just now/);
    // What was said before R9, and is no longer true: the old reading is not in the sum.
    expect(alert).not.toHaveTextContent(/comparison uses|may be out of date/);
    // The rest of the comparison stands on the venue that was read.
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
  });

  it('read_failed and never read: why, and that its coins are left out', async () => {
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000')],
        exchanges: [failedBalances('bingx', 'schema', null), exchangeBalances()],
      }),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    expect(alert.textContent).toBe(
      'The balances at BingX could not be read. BingX answered in a shape this application ' +
        'could not read. No balances have been read from BingX, so the coins held there are ' +
        'left out of the comparison.',
    );
    expect(alert.querySelector('time')).toBeNull();
    expect(alert).not.toHaveTextContent(/last good reading/);
  });

  it.each(['auth', 'insufficient_scope'] as const)(
    'read_failed with a refused key (%s): scheduled syncs will not ask again, and how to retry',
    async (kind) => {
      openDashboard({
        exchanges: bothVenues(),
        reconciliation: reconciliation({
          assets: [matchedAsset('BTC', '1.500000000000000000')],
          exchanges: [failedBalances('bingx', kind, null), exchangeBalances()],
        }),
      });

      const alert = within(await holdingsCheck()).getByRole('alert');
      expect(alert.textContent).toBe(
        'The balances at BingX could not be read. BingX refused the API key for the balance ' +
          'read. Scheduled syncs will not ask again; a sync from the Exchanges page retries ' +
          'once the key is fixed. No balances have been read from BingX, so the coins held ' +
          'there are left out of the comparison.',
      );
    },
  );

  it('read_failed with a refused key and a previous reading: both, in that order', async () => {
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcOnBingx(failedBalances('bitget', 'auth')),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    expect(alert).toHaveTextContent(
      'The balances at Bitget could not be read. Bitget refused the API key for the balance ' +
        'read. Scheduled syncs will not ask again; a sync from the Exchanges page retries once ' +
        'the key is fixed. Its last good reading, from',
    );
    expect(alert).toHaveTextContent(
      /is not used, so the coins held there are left out of the comparison\.$/,
    );
    expect(alert.querySelector('time')?.getAttribute('datetime')).toBe(OLD_BALANCES_READ_AT);
  });

  it('read_failed for a defect of ours is not blamed on the venue, nor called a stopped sync', async () => {
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcOnBingx(failedBalances('bitget', 'internal')),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    expect(alert).toHaveTextContent(
      'The balances at Bitget could not be read. A defect in this application stopped the ' +
        'balances from being read. The container log has the details. Its last good reading, ' +
        'from',
    );
    expect(alert).not.toHaveTextContent(/stopped the sync/);
  });

  it('never_read: its balances come after its next successful sync', async () => {
    openDashboard({
      positions: emptySnapshot(),
      reconciliation: reconciliation({ exchanges: [unreadBalances('bitget')] }),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    expect(alert.textContent).toBe(
      'The balances at Bitget have not been read yet. They are read after its next successful ' +
        'sync, and until then the coins held there are left out of the comparison.',
    );
    // Not worded as a failure: nothing was attempted.
    expect(alert).not.toHaveTextContent(/could not be read/);
    expect(alert.querySelector('time')).toBeNull();
  });

  it('sync_failed: its balances were not read and are left out, with when they last were', async () => {
    inTimeZone('UTC');
    openDashboard({
      exchanges: [exchange({ exchange_key: 'bingx' }), erroredExchange('unavailable')],
      reconciliation: btcOnBingx(syncFailedBalances('bitget')),
    });

    const block = await holdingsCheck();
    const alert = within(block).getByRole('alert');
    const time = alert.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe(OLD_BALANCES_READ_AT);
    expect(time?.textContent).toMatch(/^Sep 21, 2026, 12:00\sPM$/);
    expect(alert.textContent).toBe(
      'The last sync of Bitget failed, so its balances were not read and are left out of the ' +
        `comparison. They were last read on ${time?.textContent ?? ''}.`,
    );
    expect(alert).not.toHaveTextContent(/ago|just now/);
  });

  it('sync_failed leaves out a reading however recent: minutes old, and still not compared', async () => {
    inTimeZone('UTC');
    openDashboard({
      exchanges: [exchange({ exchange_key: 'bingx' }), erroredExchange('unavailable')],
      reconciliation: btcOnBingx(syncFailedBalances('bitget', BALANCES_READ_AT)),
    });

    const block = await holdingsCheck();
    const alert = within(block).getByRole('alert');
    expect(alert.querySelector('time')?.getAttribute('datetime')).toBe(BALANCES_READ_AT);
    expect(alert.querySelector('time')?.textContent).toMatch(/^Sep 24, 2026, 11:46\sAM$/);
    expect(alert).toHaveTextContent(/^The last sync of Bitget failed/);
    // 14 minutes old, and not in the list of what was compared.
    expect(readings(block).map((entry) => entry.text)).toEqual(['BingX: 16 minutes ago']);
  });

  it('out_of_date: when it was last read, and the age limit that leaves it out', async () => {
    inTimeZone('UTC');
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcOnBingx(outOfDateBalances('bitget')),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    const time = alert.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe(OLD_BALANCES_READ_AT);
    expect(alert.textContent).toBe(
      `The balances at Bitget were last read on ${time?.textContent ?? ''}. A reading older ` +
        'than 24 hours is left out of the comparison.',
    );
    expect(time?.textContent).toMatch(/^Sep 21, 2026, 12:00\sPM$/);
    // Nothing failed, and nothing says it did.
    expect(alert).not.toHaveTextContent(/could not be read|failed|ago/);
  });

  it('out_of_date states the age limit the response carries', async () => {
    const response = btcOnBingx(outOfDateBalances('bitget'));
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: reconciliation({ ...response, max_reading_age_hours: 48 }),
    });

    const alert = within(await holdingsCheck()).getByRole('alert');
    expect(alert).toHaveTextContent('A reading older than 48 hours is left out of the comparison.');
    expect(alert).not.toHaveTextContent('24 hours');
  });

  it.each([
    ['read_failed', failedBalances('bitget', 'rate_limited')],
    ['never_read', unreadBalances('bitget')],
    ['sync_failed', syncFailedBalances('bitget')],
    ['out_of_date', outOfDateBalances('bitget')],
  ] as const)(
    '%s: one alert naming Bitget, and Bitget is not among the readings compared',
    async (reason, bitget) => {
      expect(bitget.not_compared_reason).toBe(reason);
      openDashboard({ exchanges: bothVenues(), reconciliation: btcOnBingx(bitget) });

      const block = await holdingsCheck();
      const alerts = within(block).getAllByRole('alert');
      expect(alerts).toHaveLength(1);
      expect(alerts[0]).toHaveTextContent('Bitget');
      expect(alerts[0]).not.toHaveTextContent('BingX');
      // A time beside a figure that is not in the sum would read as if it were.
      expect(readings(block)).toEqual([
        { text: 'BingX: 16 minutes ago', at: OTHER_BALANCES_READ_AT },
      ]);
    },
  );

  it('a venue left out contributes nothing: its coins make the history look over, never short', async () => {
    // Bitget is the only venue and its read failed. Its last reading held the 1.5 BTC; R9 keeps
    // that out of the sum, so nothing is held anywhere that was read: 0 - 1.5 = -1.5.
    openDashboard({
      reconciliation: reconciliation({
        assets: [
          assetReconciliation({
            history_quantity: '1.500000000000000000',
            wallet_quantity: ZERO,
            exchange_quantity: ZERO,
            held_quantity: ZERO,
            difference: '-1.500000000000000000',
            status: 'history_over',
          }),
        ],
        exchanges: [failedBalances('bitget', 'unavailable')],
      }),
    });

    const block = await holdingsCheck();
    expect(within(block).getByRole('alert')).toHaveTextContent(
      'The balances at Bitget could not be read.',
    );
    const details = overDisclosure(block);
    expect(details.open).toBe(false);
    expect(readTable(within(details).getByRole('table')).rows).toEqual([
      {
        asset: 'BTC',
        text: ['1.5', '0', '0', '-1.5'],
        values: ['1.500000000000000000', ZERO, ZERO, '-1.500000000000000000'],
      },
    ]);
    // Nothing is called a finding, no badge is drawn, and no reading is listed as compared.
    expect(within(block).queryByRole('heading', { level: 4 })).toBeNull();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(within(block).queryByText('Balances last read:')).toBeNull();
  });

  it('one stale wallet, in the singular, with the age limit', async () => {
    openDashboard({ reconciliation: btcShort({ wallets: walletReadings(2, { stale: 1 }) }) });

    expect(within(await holdingsCheck()).getByRole('alert').textContent).toBe(
      '1 wallet was last read more than 24 hours ago, so the coins in it are left out of the ' +
        'comparison.',
    );
  });

  it('several stale wallets, in the plural, with the age limit the response carries', async () => {
    openDashboard({
      reconciliation: btcShort({
        max_reading_age_hours: 36,
        wallets: walletReadings(2, { stale: 3 }),
      }),
    });

    expect(within(await holdingsCheck()).getByRole('alert').textContent).toBe(
      '3 wallets were last read more than 36 hours ago, so the coins in them are left out of ' +
        'the comparison.',
    );
  });

  it('one unread wallet, in the singular', async () => {
    openDashboard({ reconciliation: btcShort({ wallets: walletReadings(2, { unread: 1 }) }) });

    expect(within(await holdingsCheck()).getByRole('alert').textContent).toBe(
      '1 wallet has not been read yet, so the coins in it are left out of the comparison.',
    );
  });

  it('several unread wallets, in the plural', async () => {
    openDashboard({ reconciliation: btcShort({ wallets: walletReadings(2, { unread: 3 }) }) });

    expect(within(await holdingsCheck()).getByRole('alert').textContent).toBe(
      '3 wallets have not been read yet, so the coins in them are left out of the comparison.',
    );
  });

  it('stale and unread wallets are two notices, each with its own count, stale first', async () => {
    openDashboard({
      reconciliation: btcShort({ wallets: walletReadings(2, { stale: 2, unread: 1 }) }),
    });

    expect(
      within(await holdingsCheck())
        .getAllByRole('alert')
        .map((alert) => alert.textContent),
    ).toEqual([
      '2 wallets were last read more than 24 hours ago, so the coins in them are left out of ' +
        'the comparison.',
      '1 wallet has not been read yet, so the coins in it are left out of the comparison.',
    ]);
  });

  it('no wallet compared: the counts alone, and no wallet among the readings', async () => {
    openDashboard({
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000')],
        wallets: walletReadings(0, { stale: 1, unread: 2 }),
      }),
    });

    const block = await holdingsCheck();
    expect(
      within(block)
        .getAllByRole('alert')
        .map((alert) => alert.textContent),
    ).toEqual([
      '1 wallet was last read more than 24 hours ago, so the coins in it are left out of the ' +
        'comparison.',
      '2 wallets have not been read yet, so the coins in them are left out of the comparison.',
    ]);
    expect(readings(block).map((entry) => entry.text)).toEqual(['Bitget: 14 minutes ago']);
  });

  it('names each source left out in its own alert: the venues in order, then the wallets', async () => {
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcShortInWallets({
        exchanges: [unreadBalances('bingx'), failedBalances('bitget', 'schema')],
        wallets: walletReadings(2, { stale: 1, unread: 1 }),
      }),
    });

    const block = await holdingsCheck();
    const alerts = within(block).getAllByRole('alert');
    expect(alerts.map((alert) => alert.tagName)).toEqual(['P', 'P', 'P', 'P']);
    expect(alerts[0]).toHaveTextContent('The balances at BingX have not been read yet.');
    expect(alerts[1]).toHaveTextContent(
      'The balances at Bitget could not be read. Bitget answered in a shape this application ' +
        'could not read. Its last good reading, from',
    );
    expect(alerts[2]).toHaveTextContent('1 wallet was last read more than 24 hours ago');
    expect(alerts[3]).toHaveTextContent('1 wallet has not been read yet');
    // A source left out hides nothing that was found: the finding is still listed below it.
    const table = shortTable(block, 1);
    expect(
      (alerts[3] ?? block).compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(readTable(table).rows[0]?.text).toEqual(['1.5', '1.62345678', '0', '+0.12345678']);
    // Only the wallets were compared, and only they are listed as read.
    expect(readings(block).map((entry) => entry.text)).toEqual([
      'Wallets (oldest reading): 20 minutes ago',
    ]);
  });
});

/**
 * BTC's 1.5 sits on Bitget, read and compared, so the asset matches whatever the wallets'
 * state is: the notices under test are all that the chains left out add to the block.
 */
function btcOnBitget(
  failedChains: readonly FailedChainResponse[],
  compared = 0,
): ReconciliationResponse {
  return reconciliation({
    assets: [matchedAsset('BTC', '1.500000000000000000')],
    wallets: walletReadings(compared, { failedChains }),
  });
}

/** The text of every alert inside `element`, in document order. */
function alertTexts(element: HTMLElement): string[] {
  return within(element)
    .queryAllByRole('alert')
    .map((alert) => alert.textContent);
}

/**
 * Records every `console.error` while a test runs, each call as one string. React reports two
 * children with the same key there and nowhere else: both are still drawn on the first render,
 * so nothing on screen shows it.
 */
function recordConsoleErrors(): { messages: () => string[]; restore: () => void } {
  const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);

  return {
    messages: () => spy.mock.calls.map((call) => call.map(String).join(' ')),
    restore: () => {
      spy.mockRestore();
    },
  };
}

const DUPLICATE_KEY = 'two children with the same key';

describe('HoldingsCheck: wallets left out because their chain failed (spec 028)', () => {
  it('one wallet: names the chain by its display name, in the singular, in an alert', async () => {
    openDashboard({ reconciliation: btcOnBitget([failedChain('bitcoin', 1)]) });

    const block = await holdingsCheck();
    const alert = within(block).getByRole('alert');
    expect(alert.tagName).toBe('P');
    expect(alert.textContent).toBe(
      'The last balance sync that finished could not read Bitcoin, so the coins in ' +
        '1 wallet on it are left out of the comparison.',
    );
    // No instant and no reason: nothing here ticks, and the Value section says why.
    expect(alert.querySelector('time')).toBeNull();
    expect(alert).not.toHaveTextContent(/ago|just now|hours/);
    // A wallet left out hides nothing that was found and invents nothing: the rest stands.
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
    expect(within(block).queryByRole('heading', { level: 4 })).toBeNull();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
  });

  it('several wallets: the number of them, in the plural', async () => {
    openDashboard({ reconciliation: btcOnBitget([failedChain('kaspa', 3)]) });

    expect(alertTexts(await holdingsCheck())).toEqual([
      'The last balance sync that finished could not read Kaspa, so the coins in ' +
        '3 wallets on it are left out of the comparison.',
    ]);
  });

  it('two failed chains: one alert each, in the endpoint order, each with its own count', async () => {
    const errors = recordConsoleErrors();
    try {
      openDashboard({
        reconciliation: btcOnBitget([failedChain('bitcoin', 2), failedChain('kaspa', 1)]),
      });

      const block = await holdingsCheck();
      expect(alertTexts(block)).toEqual([
        'The last balance sync that finished could not read Bitcoin, so the coins in ' +
          '2 wallets on it are left out of the comparison.',
        'The last balance sync that finished could not read Kaspa, so the coins in ' +
          '1 wallet on it are left out of the comparison.',
      ]);
      // `chain_failed` is 3, and it is not rendered: the entries carry the counts.
      expect(block).not.toHaveTextContent(/3 wallets/);
      await settle();
      // Two notices of one kind are two list children: each needs a key of its own.
      expect(errors.messages().filter((message) => message.includes(DUPLICATE_KEY))).toEqual([]);
    } finally {
      errors.restore();
    }
  });

  it('the control: two children with one key are reported where that test looks', () => {
    const errors = recordConsoleErrors();
    try {
      render(
        <ul>
          {['a', 'a'].map((key, index) => (
            <li key={key}>{String(index)}</li>
          ))}
        </ul>,
      );

      expect(errors.messages().some((message) => message.includes(DUPLICATE_KEY))).toBe(true);
    } finally {
      errors.restore();
    }
  });

  it('a chain this build has no name for is named by its raw key', async () => {
    openDashboard({ reconciliation: btcOnBitget([failedChain('litecoin', 2)]) });

    expect(alertTexts(await holdingsCheck())).toEqual([
      'The last balance sync that finished could not read litecoin, so the coins in ' +
        '2 wallets on it are left out of the comparison.',
    ]);
  });

  it('comes after the venues and before the stale wallets, then the unread ones', async () => {
    const errors = recordConsoleErrors();
    try {
      openDashboard({
        exchanges: bothVenues(),
        reconciliation: btcShortInWallets({
          exchanges: [unreadBalances('bingx'), failedBalances('bitget', 'schema')],
          wallets: walletReadings(2, {
            stale: 1,
            unread: 1,
            failedChains: [failedChain('bitcoin', 2), failedChain('kaspa', 1)],
          }),
        }),
      });

      const block = await holdingsCheck();
      const alerts = within(block).getAllByRole('alert');
      expect(alerts.map((alert) => alert.tagName)).toEqual(['P', 'P', 'P', 'P', 'P', 'P']);
      expect(alerts[0]).toHaveTextContent('The balances at BingX have not been read yet.');
      expect(alerts[1]).toHaveTextContent('The balances at Bitget could not be read.');
      expect(alerts[2]?.textContent).toBe(
        'The last balance sync that finished could not read Bitcoin, so the coins in ' +
          '2 wallets on it are left out of the comparison.',
      );
      expect(alerts[3]?.textContent).toBe(
        'The last balance sync that finished could not read Kaspa, so the coins in ' +
          '1 wallet on it are left out of the comparison.',
      );
      expect(alerts[4]?.textContent).toBe(
        '1 wallet was last read more than 24 hours ago, so the coins in it are left out of the ' +
          'comparison.',
      );
      expect(alerts[5]?.textContent).toBe(
        '1 wallet has not been read yet, so the coins in it are left out of the comparison.',
      );
      // The finding of the wallets that were compared is still listed below the notices.
      const table = shortTable(block, 1);
      expect(
        (alerts[5] ?? block).compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
      expect(readings(block).map((entry) => entry.text)).toEqual([
        'Wallets (oldest reading): 20 minutes ago',
      ]);
      await settle();
      expect(errors.messages().filter((message) => message.includes(DUPLICATE_KEY))).toEqual([]);
    } finally {
      errors.restore();
    }
  });

  it('every wallet left out: the notice alone, and no wallet among the readings', async () => {
    openDashboard({ reconciliation: btcOnBitget([failedChain('bitcoin', 2)]) });

    const block = await holdingsCheck();
    expect(alertTexts(block)).toHaveLength(1);
    expect(readings(block)).toEqual([{ text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT }]);
  });

  it('beside wallets that were compared: their oldest reading is still listed', async () => {
    openDashboard({ reconciliation: btcOnBitget([failedChain('kaspa', 1)], 2) });

    const block = await holdingsCheck();
    expect(alertTexts(block)).toEqual([
      'The last balance sync that finished could not read Kaspa, so the coins in ' +
        '1 wallet on it are left out of the comparison.',
    ]);
    expect(readings(block)).toEqual([
      { text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT },
      { text: 'Wallets (oldest reading): 20 minutes ago', at: WALLETS_OBSERVED_AT },
    ]);
  });

  it('with no failed chain the block says nothing of one: the notices are what they were', async () => {
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcShortInWallets({
        exchanges: [unreadBalances('bingx'), exchangeBalances()],
        wallets: walletReadings(2, { stale: 2, unread: 1, failedChains: [] }),
      }),
    });

    const block = await holdingsCheck();
    expect(alertTexts(block)).toEqual([
      'The balances at BingX have not been read yet. They are read after its next successful ' +
        'sync, and until then the coins held there are left out of the comparison.',
      '2 wallets were last read more than 24 hours ago, so the coins in them are left out of ' +
        'the comparison.',
      '1 wallet has not been read yet, so the coins in it are left out of the comparison.',
    ]);
    expect(block).not.toHaveTextContent(/balance sync that finished/);
  });

  it('follows the endpoint from one poll to the next: a chain that recovers loses its notice', async () => {
    fakePolling();
    const errors = recordConsoleErrors();
    try {
      const { accounting } = openDashboard({
        reconciliation: btcOnBitget([failedChain('bitcoin', 2), failedChain('kaspa', 1)]),
      });
      const block = await holdingsCheck();
      expect(alertTexts(block)).toHaveLength(2);

      // The next balance run read Bitcoin: its two wallets are compared again.
      accounting.setReconciliation(btcOnBitget([failedChain('kaspa', 1)], 2));
      nextPoll();
      await waitFor(() => {
        expect(alertTexts(block)).toEqual([
          'The last balance sync that finished could not read Kaspa, so the coins in ' +
            '1 wallet on it are left out of the comparison.',
        ]);
      });

      // And the one after it read Kaspa too.
      accounting.setReconciliation(btcOnBitget([], 3));
      nextPoll();
      await waitFor(() => {
        expect(alertTexts(block)).toEqual([]);
      });
      expect(errors.messages().filter((message) => message.includes(DUPLICATE_KEY))).toEqual([]);
    } finally {
      errors.restore();
    }
  });

  it('is still said when the history is stale: the sources first, then why nothing is compared', async () => {
    openDashboard({
      positions: investedPortfolio({ last_recompute: failedRecompute() }),
      reconciliation: investedPortfolioGaps({
        last_recompute: failedRecompute(),
        wallets: walletReadings(3, { failedChains: [failedChain('kaspa', 1)] }),
      }),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    const alerts = alertTexts(block);
    expect(alerts).toHaveLength(2);
    expect(alerts[0]).toBe(
      'The last balance sync that finished could not read Kaspa, so the coins in ' +
        '1 wallet on it are left out of the comparison.',
    );
    expect(alerts[1]).toMatch(STALE_HISTORY);
  });
});

describe('HoldingsCheck: held exceeds history', () => {
  it('lists the assets under the columns the spec names, each figure from the exact wire string', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    expect(
      within(block).getByRole('heading', { level: 4, name: 'Held exceeds history (3)' }),
    ).toBeInTheDocument();
    const { columns, rows } = readTable(shortTable(block, 3));

    expect(columns).toEqual(COLUMNS);
    // The endpoint's order. KAS matches and SOL is over: neither is here.
    expect(rows.map((row) => row.asset)).toEqual(['BTC', 'ETH', 'XRP']);
    expect(rows[0]).toEqual({
      asset: 'BTC',
      text: ['1.5', '1.62345678', '0.25', '+0.37345678'],
      values: [BTC_BEYOND.history, BTC_BEYOND.wallets, BTC_BEYOND.exchanges, BTC_BEYOND.difference],
    });
    expect(rows[2]).toEqual({
      asset: 'XRP',
      text: ['0', '0', '12.5', '+12.5'],
      values: [XRP_LEFT.history, XRP_LEFT.wallets, XRP_LEFT.exchanges, XRP_LEFT.difference],
    });
  });

  it('keeps every one of 18 places a double cannot hold', async () => {
    // 3.141592653589793238 as a double is 3.141592653589793, and the difference
    // 0.423310825130748003 is 0.423310825130748: the trailing digits would vanish.
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const eth = readTable(shortTable(await holdingsCheck(), 3)).rows[1];
    expect(eth?.values).toEqual([
      '2.718281828459045235',
      '0.000000000000000000',
      '3.141592653589793238',
      '0.423310825130748003',
    ]);
    expect(eth?.values).toEqual([
      ETH_BEYOND.history,
      ETH_BEYOND.wallets,
      ETH_BEYOND.exchanges,
      ETH_BEYOND.difference,
    ]);
    // Shown to 8 places, rounded; the exact value is in the attribute.
    expect(eth?.text).toEqual(['2.71828183', '0', '3.14159265', '+0.42331083']);
  });

  it('writes the sign of the difference out, on every row', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const { rows } = readTable(shortTable(await holdingsCheck(), 3));
    for (const row of rows) {
      expect(row.text[3]).toMatch(/^\+\d/);
      // The other columns are quantities, and unsigned.
      expect(row.text.slice(0, 3).join(' ')).not.toMatch(/[+-]/);
    }
  });

  it('says what it means, what it does to the figures, and what to rule out first', async () => {
    openDashboard({ reconciliation: btcShort() });

    const block = await holdingsCheck();
    const guidance = within(block).getByText(GUIDANCE);
    const table = shortTable(block, 1);
    // Above the table, as the reason to read it.
    expect(guidance.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // Spec 027: the pointer to the documentation is gone, now that there is a page to link to.
    expect(guidance.tagName).toBe('P');
    expect(guidance).not.toHaveTextContent('docs/accounting.md');
    expect(block).not.toHaveTextContent('docs/accounting.md');
    expect(block).not.toHaveTextContent('Recording what the history does not show');
    expect(within(block).getByText(DIFFERENCE_LEGEND)).toBeInTheDocument();
    // R10: it says how old each reading is "is shown below the lists", and it is - a finding
    // needs something held, so a compared source, so a reading to list.
    expect(guidance).toHaveTextContent('How old each reading is, is shown below the lists.');
    const ages = within(block).getByRole('list');
    expect(table.compareDocumentPosition(ages) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(readings(block).map((entry) => entry.text)).toEqual([
      'Bitget: 14 minutes ago',
      'Wallets (oldest reading): 20 minutes ago',
    ]);
    // Beside a finding, nothing says the quantities match.
    expect(block).not.toHaveTextContent(ALL_MATCH);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
  });

  it('links the asset to the page that records the missing coins (spec 027, criterion 15)', async () => {
    openDashboard({ reconciliation: btcShort() });

    const block = await holdingsCheck();
    const line = recordLine(block);
    // After the guidance, which says what to rule out before recording anything.
    const guidance = within(block).getByText(GUIDANCE);
    expect(guidance.compareDocumentPosition(line) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(line).toHaveTextContent(`${RECORD_PROMPT} BTC`);
    // Inside the finding's own box, not loose in the block.
    expect(line.closest('.holdings-short')).not.toBeNull();
    // The asset is the link's text, and the only link in the whole block.
    expect(linksIn(line)).toEqual([{ text: 'BTC', href: '/adjustments?asset=BTC' }]);
    expect(linksIn(block)).toEqual([{ text: 'BTC', href: '/adjustments?asset=BTC' }]);
  });

  it('links each asset held beyond its history, in the order they are listed, and no other', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    const line = recordLine(block);

    // BTC, ETH and XRP are short. KAS matches and SOL is over: neither has a link, here or
    // anywhere else in the block.
    expect(linksIn(line)).toEqual([
      { text: 'BTC', href: '/adjustments?asset=BTC' },
      { text: 'ETH', href: '/adjustments?asset=ETH' },
      { text: 'XRP', href: '/adjustments?asset=XRP' },
    ]);
    expect(linksIn(block)).toEqual(linksIn(line));
    expect(line).toHaveTextContent(`${RECORD_PROMPT} BTC, ETH, XRP`);
    expect(linksIn(overDisclosure(block))).toEqual([]);
    // The tables name the assets and link nothing.
    expect(linksIn(shortTable(block, 3))).toEqual([]);
  });

  it('carries the asset over and nothing else: never the difference shown beside it', async () => {
    // Spec 027, non-goals: the quantity is never prefilled. The difference can include coins
    // in transit between two readings (spec 025, R9 and R10).
    openDashboard({ reconciliation: btcShort() });

    const [link] = within(recordLine(await holdingsCheck())).getAllByRole('link');
    const href = link?.getAttribute('href') ?? '';
    const [path, query] = href.split('?');

    expect(path).toBe('/adjustments');
    expect([...new URLSearchParams(query).entries()]).toEqual([['asset', 'BTC']]);
    expect(href).not.toContain(BTC_BEYOND.difference);
    expect(href).not.toContain('0.37345678');
  });

  it.each([
    ['A&B', '/adjustments?asset=A%26B'],
    ['$MYRO', '/adjustments?asset=%24MYRO'],
    ['T#1', '/adjustments?asset=T%231'],
    ['L 2', '/adjustments?asset=L+2'],
    ['C+', '/adjustments?asset=C%2B'],
    ['A&B#C', '/adjustments?asset=A%26B%23C'],
    ['Q?=', '/adjustments?asset=Q%3F%3D'],
  ])(
    'encodes the asset %j in the link, so it cannot change what the link means',
    async (asset, href) => {
      // A venue's symbol is data. Concatenated into the URL, `A&B` would be an asset `A` and a
      // parameter `B`, and `T#1` an asset `T` and a fragment.
      openDashboard({
        positions: emptySnapshot(),
        reconciliation: reconciliation({
          assets: [
            assetReconciliation({
              asset,
              history_quantity: ZERO,
              wallet_quantity: ZERO,
              exchange_quantity: '2.000000000000000000',
              held_quantity: '2.000000000000000000',
              difference: '2.000000000000000000',
            }),
          ],
        }),
      });

      const line = recordLine(await holdingsCheck());

      expect(linksIn(line)).toEqual([{ text: asset, href }]);
      // And the page that reads it back gets the asset exactly.
      expect(new URLSearchParams(href.slice(href.indexOf('?'))).get('asset')).toBe(asset);
    },
  );

  it('the link opens the adjustments page with the asset filled in, and nothing else', async () => {
    const adjustments = fakeAdjustments({ firstTrades: firstTrades([firstTrade('BTC')]) });
    const { user } = openDashboard({
      reconciliation: btcShort(),
      overrides: adjustments.handlers,
    });

    await user.click(within(recordLine(await holdingsCheck())).getByRole('link', { name: 'BTC' }));

    const form = await screen.findByRole('form', { name: 'Record an adjustment' });
    expect(currentPath()).toBe('/adjustments?asset=BTC');
    expect(within(form).getByLabelText('Asset')).toHaveValue('BTC');
    // The difference the block showed is not carried over: the owner states the quantity.
    expect(within(form).getByLabelText('Quantity')).toHaveValue('');
    expect(within(form).getByLabelText('Unit cost (USD)')).toHaveValue('');
    expect(within(form).getByLabelText('Acquired on')).toHaveValue('');
    expect(within(form).getByLabelText('Note')).toHaveValue('');
  });

  it('an asset whose symbol needs encoding arrives on the page as it is spelled', async () => {
    // `&` would start a second parameter and `#` a fragment: either one, unencoded, would
    // cut the asset short on its way to the field.
    const adjustments = fakeAdjustments();
    const { user } = openDashboard({
      positions: emptySnapshot(),
      reconciliation: reconciliation({
        assets: [
          assetReconciliation({
            asset: 'A&B#C',
            history_quantity: ZERO,
            wallet_quantity: ZERO,
            exchange_quantity: '2.000000000000000000',
            held_quantity: '2.000000000000000000',
            difference: '2.000000000000000000',
          }),
        ],
      }),
      overrides: adjustments.handlers,
    });

    await user.click(
      within(recordLine(await holdingsCheck())).getByRole('link', { name: 'A&B#C' }),
    );

    const form = await screen.findByRole('form', { name: 'Record an adjustment' });
    expect(currentPath()).toBe('/adjustments?asset=A%26B%23C');
    expect(within(form).getByLabelText('Asset')).toHaveValue('A&B#C');
  });

  it('puts the table in a keyboard-reachable region named by its heading', async () => {
    openDashboard({ reconciliation: btcShort() });

    const block = await holdingsCheck();
    const scroller = within(block).getByRole('region', { name: 'Held exceeds history (1)' });
    expect(scroller).toHaveAttribute('tabindex', '0');
    expect(scroller).toContainElement(shortTable(block, 1));
    // The finding is stated in the open: not inside a disclosure.
    expect(scroller.closest('details')).toBeNull();
    expect(block.querySelector('details')).toBeNull();
  });

  it('lists an asset held in a wallet and never traded, which has no position at all', async () => {
    openDashboard({
      positions: emptySnapshot(),
      reconciliation: reconciliation({ assets: [kasNeverTraded()], wallets: walletReadings(1) }),
    });

    const block = await holdingsCheck();
    expect(readTable(shortTable(block, 1)).rows).toEqual([
      {
        asset: 'KAS',
        text: ['0', '250,000', '0', '+250,000'],
        values: [ZERO, '250000.000000000000000000', ZERO, '250000.000000000000000000'],
      },
    ]);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
  });

  it('shows a balance too small for eight places as a bound, never as a zero', async () => {
    // Dust a venue lists: 0.000000001 DOGE against no history. Difference +0.000000001.
    const dust = '0.000000001000000000';
    openDashboard({
      positions: emptySnapshot(),
      reconciliation: reconciliation({
        assets: [
          assetReconciliation({
            asset: 'DOGE',
            history_quantity: ZERO,
            wallet_quantity: ZERO,
            exchange_quantity: dust,
            held_quantity: dust,
            difference: dust,
          }),
        ],
      }),
    });

    expect(readTable(shortTable(await holdingsCheck(), 1)).rows).toEqual([
      {
        asset: 'DOGE',
        text: ['0', '0', '< 0.00000001', '< +0.00000001'],
        values: [ZERO, ZERO, dust, dust],
      },
    ]);
  });

  it('puts only wire strings in every <data value> of the block', async () => {
    const response = investedPortfolioGaps();
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: response,
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    const sent = wireStrings(response.assets);
    const values = dataValues(block);

    // Three rows held beyond the history and one above the balances, four quantities each.
    expect(values).toHaveLength(16);
    for (const value of values) {
      expect(sent).toContain(value);
    }
  });
});

describe('HoldingsCheck: history above the balances read', () => {
  it('lists them in a disclosure that is closed, with their count in its summary', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const details = overDisclosure(await holdingsCheck());
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe(
      'History above the balances read (1)',
    );
    const { columns, rows } = readTable(within(details).getByRole('table'));
    expect(columns).toEqual(COLUMNS);
    expect(rows).toEqual([
      {
        asset: 'SOL',
        text: ['10', '0', '7.5', '-2.5'],
        values: [SOL_OVER.history, SOL_OVER.wallets, SOL_OVER.exchanges, SOL_OVER.difference],
      },
    ]);
    expect(within(details).getByText(DIFFERENCE_LEGEND)).toBeInTheDocument();
  });

  it('opens when its summary is activated', async () => {
    const { user } = openDashboard({ reconciliation: btcOver() });

    const details = overDisclosure(await holdingsCheck());
    expect(details.open).toBe(false);
    const summary = details.querySelector('summary');
    if (summary === null) {
      throw new Error('The disclosure has no summary.');
    }

    await user.click(summary);

    expect(details.open).toBe(true);
    expect(
      within(details).getByRole('table', { name: 'History above the balances read (1)' }),
    ).toBeInTheDocument();
  });

  it('names the causes, says it cannot tell them apart, and raises no alert for it (R9)', async () => {
    openDashboard({ reconciliation: btcOver() });

    const block = await holdingsCheck();
    const details = overDisclosure(block);
    expect(within(details).getByText(OVER_EXPLANATION)).toBeInTheDocument();
    // A sale the import did not see is among the causes, so nothing promises there is no gap.
    expect(details).toHaveTextContent('a sale or conversion the import did not see');
    expect(details).not.toHaveTextContent(/Nothing needs correcting/);
    expect(within(block).queryByRole('alert')).toBeNull();
    // It is not the finding: no heading of its own, no guidance to record anything, no badge.
    expect(within(block).queryByRole('heading', { level: 4 })).toBeNull();
    expect(block).not.toHaveTextContent(/opening balance/);
    // Quiet is not "matches": a quantity that is listed does not match, so neither line is said.
    expect(block).not.toHaveTextContent(ALL_MATCH);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
    expect(readTable(within(details).getByRole('table')).rows).toEqual([
      {
        asset: 'BTC',
        text: ['1.5', '0', '1', '-0.5'],
        values: [BTC_BELOW.history, BTC_BELOW.wallets, BTC_BELOW.exchanges, BTC_BELOW.difference],
      },
    ]);
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
  });

  it('counts every asset in it', async () => {
    // KAS's wallet has not been read, so nothing of its 1500 is held anywhere that was.
    openDashboard({
      positions: investedPortfolio(),
      exchanges: bothVenues(),
      reconciliation: investedPortfolioGaps({
        assets: [
          btcBeyondPosition(),
          matchedAsset('ETH', '2.718281828459045235'),
          assetReconciliation({
            asset: 'KAS',
            history_quantity: '1500.000000000000000000',
            wallet_quantity: ZERO,
            exchange_quantity: ZERO,
            held_quantity: ZERO,
            difference: '-1500.000000000000000000',
            status: 'history_over',
          }),
          solOver(),
        ],
        wallets: walletReadings(2, { unread: 1 }),
      }),
    });

    const block = await holdingsCheck();
    const details = overDisclosure(block);
    expect(details.querySelector('summary')?.textContent).toBe(
      'History above the balances read (2)',
    );
    const { rows } = readTable(within(details).getByRole('table'));
    expect(rows.map((row) => [row.asset, row.text[3]])).toEqual([
      ['KAS', '-1,500'],
      ['SOL', '-2.5'],
    ]);
    expect(rows[0]?.values[3]).toBe('-1500.000000000000000000');
    expect(shortTable(block, 1)).toBeInTheDocument();
  });

  it('comes after the finding, which stands in the open above it', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    const finding = within(block).getByRole('heading', { level: 4 });
    const details = overDisclosure(block);
    expect(
      finding.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(finding.closest('details')).toBeNull();
    expect(details).not.toContainElement(shortTable(block, 3));
    // With a list on screen, neither quiet line is said.
    expect(block).not.toHaveTextContent(ALL_MATCH);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
  });
});

describe('HoldingsCheck: the match and the empty lines', () => {
  it('says every quantity matches when no asset is in either list, and renders no amount', async () => {
    openDashboard({ positions: investedPortfolio() });

    const block = await holdingsCheck();
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
    expect(within(block).queryByRole('table')).toBeNull();
    expect(within(block).queryByRole('heading', { level: 4 })).toBeNull();
    expect(block.querySelector('details')).toBeNull();
    expect(block.querySelector('data')).toBeNull();
    expect(within(block).queryByRole('alert')).toBeNull();
  });

  it('calls a difference inside the tolerance a match, and does not list it', async () => {
    // KAS: 1000 in the history, 995 on the exchange. -5 is half a percent.
    openDashboard({
      positions: kasLossPortfolio(),
      reconciliation: reconciliation({
        assets: [
          assetReconciliation({
            asset: 'KAS',
            history_quantity: '1000.000000000000000000',
            wallet_quantity: ZERO,
            exchange_quantity: '995.000000000000000000',
            held_quantity: '995.000000000000000000',
            difference: '-5.000000000000000000',
            status: 'match',
          }),
        ],
      }),
    });

    const block = await holdingsCheck();
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
    expect(within(block).queryByRole('table')).toBeNull();
    expect(block.querySelector('details')).toBeNull();
    expect(await positionHeader('KAS')).toHaveAccessibleName('KAS');
  });

  it('says there is nothing to compare when no asset is compared at all', async () => {
    const { accounting } = openDashboard({ positions: emptySnapshot() });

    const block = await holdingsCheck();
    expect(accounting.reconciliation().assets).toEqual([]);
    expect(within(block).getByText(NOTHING_TO_COMPARE)).toBeInTheDocument();
    expect(block).not.toHaveTextContent(ALL_MATCH);
    expect(block.querySelector('data')).toBeNull();
  });

  it('says there is nothing to compare over a snapshot of stablecoin trades only', async () => {
    // Cash assets are never compared, so a history of them compares nothing.
    openDashboard({
      positions: stablecoinOnlySnapshot(),
      portfolio: emptyPortfolio(),
      reconciliation: reconciliation(),
    });

    const block = await holdingsCheck();
    expect(within(block).getByText(NOTHING_TO_COMPARE)).toBeInTheDocument();
    // The venue was read all the same, and the block says when.
    expect(readings(block)).toEqual([{ text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT }]);
  });
});

describe('HoldingsCheck: how old each compared reading is', () => {
  it('names each venue that was compared with when, then the oldest wallet reading', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    expect(within(block).getByText('Balances last read:')).toBeInTheDocument();
    expect(readings(block)).toEqual([
      { text: 'BingX: 16 minutes ago', at: OTHER_BALANCES_READ_AT },
      { text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT },
      { text: 'Wallets (oldest reading): 20 minutes ago', at: WALLETS_OBSERVED_AT },
    ]);
  });

  it('keeps the phrase out of any live region: it ticks', async () => {
    openDashboard({ reconciliation: btcShort() });

    const list = within(await holdingsCheck()).getByRole('list');
    expect(list.querySelectorAll('time')).toHaveLength(2);
    expect(list.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
  });

  it('does not list the old reading a failed venue keeps: it is not in the sum (R9)', async () => {
    // Before R9 this line read "Bitget: 3 days ago", beside figures that still counted it.
    openDashboard({
      exchanges: bothVenues(),
      reconciliation: btcOnBingx(failedBalances('bitget', 'rate_limited')),
    });

    const block = await holdingsCheck();
    expect(readings(block)).toEqual([
      { text: 'BingX: 16 minutes ago', at: OTHER_BALANCES_READ_AT },
    ]);
    expect(within(block).getByRole('list')).not.toHaveTextContent(/Bitget|3 days ago/);
  });

  it('lists a compared reading at the very edge of the age limit', async () => {
    openDashboard({
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000')],
        exchanges: [exchangeBalances({ balances_read_at: EDGE_BALANCES_READ_AT })],
      }),
    });

    const block = await holdingsCheck();
    expect(readings(block)).toEqual([{ text: 'Bitget: 1 day ago', at: EDGE_BALANCES_READ_AT }]);
    expect(within(block).queryByRole('alert')).toBeNull();
  });

  it('leaves out a venue never read, and the wallets when none is compared', async () => {
    openDashboard({
      positions: emptySnapshot(),
      exchanges: bothVenues(),
      reconciliation: reconciliation({
        exchanges: [failedBalances('bingx', 'auth', null), exchangeBalances()],
        wallets: walletReadings(0, { stale: 1, unread: 1 }),
      }),
    });

    expect(readings(await holdingsCheck())).toEqual([
      { text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT },
    ]);
  });

  it('says nothing about readings when every source is left out', async () => {
    openDashboard({
      positions: emptySnapshot(),
      reconciliation: reconciliation({
        exchanges: [outOfDateBalances('bitget')],
        wallets: walletReadings(0, { stale: 2 }),
      }),
    });

    const block = await holdingsCheck();
    expect(within(block).getAllByRole('alert')).toHaveLength(2);
    expect(within(block).queryByText('Balances last read:')).toBeNull();
    expect(within(block).queryByRole('list')).toBeNull();
  });

  it('says nothing about readings when nothing has been read', async () => {
    openDashboard({ positions: emptySnapshot(), portfolio: emptyPortfolio(), exchanges: [] });

    const block = await holdingsCheck();
    expect(within(block).getByText(NOTHING_TO_COMPARE)).toBeInTheDocument();
    expect(within(block).queryByText('Balances last read:')).toBeNull();
    expect(within(block).queryByRole('list')).toBeNull();
    expect(block.querySelector('time')).toBeNull();
  });
});

describe('PositionTable: the "Held exceeds history" badge', () => {
  it('marks each held position whose asset is history_short, and no other', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    await holdingsCheck();
    // After the flags and "Not in totals", so the name reads in the order the row shows them.
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    expect(await positionHeader('ETH')).toHaveAccessibleName(
      `ETH History incomplete Not in totals ${BADGE}`,
    );
    // KAS matches, inside the tolerance; SOL's history is above the balances.
    expect(await positionHeader('KAS')).toHaveAccessibleName('KAS Unknown cost Not in totals');
    expect(await positionHeader('SOL')).toHaveAccessibleName(
      'SOL Fee not valued Unknown cost Not in totals',
    );
    expect(within(await positionsTable()).getAllByRole('link', { name: BADGE })).toHaveLength(2);
  });

  it('links to the heading of the block that lists the asset', async () => {
    openDashboard({ reconciliation: btcShort() });

    const block = await holdingsCheck();
    const link = within(await positionHeader('BTC')).getByRole('link', { name: BADGE });
    expect(link).toHaveAttribute('href', '#holdings-check');
    // The target exists, once, and it is the block's heading.
    const targets = document.querySelectorAll('[id="holdings-check"]');
    expect(targets).toHaveLength(1);
    expect(targets[0]).toBe(within(block).getByRole('heading', { level: 3 }));
  });

  it('is explained in the legend, after the flags', async () => {
    openDashboard({
      positions: investedPortfolio(),
      reconciliation: investedPortfolioGaps(),
      exchanges: bothVenues(),
    });

    await holdingsCheck();
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    const entries = legend(await investedRegion());
    expect(entries.map((entry) => entry.badge)).toEqual([
      'History incomplete',
      'Fee not valued',
      'Unknown cost',
      BADGE,
    ]);
    expect(entries.at(-1)?.explanation).toBe(BADGE_EXPLANATION);
  });

  it('brings a legend of its own when no position carries a flag', async () => {
    openDashboard({ reconciliation: btcShort() });

    await holdingsCheck();
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    expect(legend(await investedRegion())).toEqual([
      { badge: BADGE, explanation: BADGE_EXPLANATION },
    ]);
    // In the legend it is a word, not a link: the link is on the row.
    expect(within(await investedRegion()).getAllByRole('link', { name: BADGE })).toHaveLength(1);
  });

  it('is absent, with its legend line, when the asset matches', async () => {
    const { accounting } = openDashboard();

    const block = await holdingsCheck();
    await reconciliationRead(accounting);
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    const invested = await investedRegion();
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    expect(legend(invested)).toEqual([]);
    expect(invested).not.toHaveTextContent(BADGE);
  });

  it('is absent when the history is above the balances: that is not a finding', async () => {
    openDashboard({ reconciliation: btcOver() });

    await holdingsCheck();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    const invested = await investedRegion();
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    expect(legend(invested)).toEqual([]);
  });

  it('is absent while the reconciliation is loading, and appears when it lands', async () => {
    let release: () => void = () => undefined;
    const { accounting } = openDashboard({
      reconciliation: btcShort(),
      before: (fakes) => {
        release = fakes.accounting.hold('reconciliation');
      },
    });

    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    await reconciliationRead(accounting);
    const invested = await investedRegion();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    expect(legend(invested)).toEqual([]);

    release();

    await waitFor(async () => {
      expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    });
    expect(legend(invested)).toEqual([{ badge: BADGE, explanation: BADGE_EXPLANATION }]);
  });

  it('is absent when the reconciliation failed: no reading, no marker', async () => {
    openDashboard({
      reconciliation: btcShort(),
      before: (fakes) => {
        fakes.accounting.fail(
          () => problem(503, 'Service Unavailable', 'The balances table is locked.'),
          'reconciliation',
        );
      },
    });

    const invested = await investedRegion();
    await within(invested).findByRole('heading', { name: LOAD_ERROR_TITLE });
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    expect(legend(invested)).toEqual([]);
  });

  it('is on no row for an asset held and never traded, which has no position', async () => {
    // KAS sits in a wallet and was never traded: listed by the block, and nowhere above it.
    openDashboard({
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000'), kasNeverTraded()],
        wallets: walletReadings(1),
      }),
    });

    const block = await holdingsCheck();
    expect(readTable(shortTable(block, 1)).rows.map((row) => row.asset)).toEqual(['KAS']);
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    const invested = await investedRegion();
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    // No marker is on screen above the block, so the legend has nothing to explain.
    expect(legend(invested)).toEqual([]);
  });
});

describe('InvestedSection: a closed position the balances still hold (spec 025, R2)', () => {
  /** BTC held and matching; XRP sold out in the history, with 12.5 left on an exchange. */
  function open(): Setup {
    return openDashboard({
      positions: positionsResponse({
        positions: [position(), xrpClosed()],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7625.500000000000000000',
        }),
      }),
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000'), xrpLeftOnExchange()],
      }),
    });
  }

  it('names the marker beside the asset in the closed line, where it has no row', async () => {
    open();

    await holdingsCheck();
    const invested = await investedRegion();
    expect(
      await within(invested).findByText(
        `1 asset no longer held is not listed: XRP (${BADGE}). Its realized P&L is in the total.`,
      ),
    ).toBeInTheDocument();
    // No row carries the badge, so nothing on the page links to the block.
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
  });

  it('explains the marker in the legend, although no row carries it', async () => {
    open();

    await holdingsCheck();
    const invested = await investedRegion();
    await within(invested).findByText(/XRP \(Held exceeds history\)/);
    expect(legend(invested)).toEqual([{ badge: BADGE, explanation: BADGE_EXPLANATION }]);
  });

  it('puts it after the flags of a closed asset that has some', async () => {
    // BGB: a fee paid in an asset never held left it history_incomplete at zero, and an
    // exchange holds 3 of it.
    openDashboard({
      positions: positionsResponse({
        positions: [bgbFeeNeverHeld(), position()],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7500.000000000000000000',
        }),
        warnings: feeInNeverHeldAsset({ charged_to: null }),
      }),
      reconciliation: reconciliation({
        assets: [
          assetReconciliation({
            asset: 'BGB',
            history_quantity: ZERO,
            wallet_quantity: ZERO,
            exchange_quantity: '3.000000000000000000',
            held_quantity: '3.000000000000000000',
            difference: '3.000000000000000000',
          }),
          matchedAsset('BTC', '1.500000000000000000'),
        ],
      }),
    });

    await holdingsCheck();
    const invested = await investedRegion();
    expect(
      await within(invested).findByText(
        `1 asset no longer held is not listed: BGB (History incomplete, ${BADGE}). ` +
          'Its realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
    expect(legend(invested).map((entry) => entry.badge)).toEqual(['History incomplete', BADGE]);
  });

  it('names no marker while the reconciliation is loading, or when it failed', async () => {
    const plain = '1 asset no longer held is not listed: XRP. Its realized P&L is in the total.';
    let release: () => void = () => undefined;
    const { accounting } = openDashboard({
      positions: positionsResponse({
        positions: [position(), xrpClosed()],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7625.500000000000000000',
        }),
      }),
      reconciliation: reconciliation({
        assets: [matchedAsset('BTC', '1.500000000000000000'), xrpLeftOnExchange()],
      }),
      before: (fakes) => {
        release = fakes.accounting.hold('reconciliation');
      },
    });

    const invested = await investedRegion();
    expect(await within(invested).findByText(plain)).toBeInTheDocument();
    await reconciliationRead(accounting);
    expect(within(invested).getByText(plain)).toBeInTheDocument();
    expect(legend(invested)).toEqual([]);

    accounting.fail(
      () => problem(503, 'Service Unavailable', 'The balances table is locked.'),
      'reconciliation',
    );
    release();

    await within(invested).findByRole('heading', { name: LOAD_ERROR_TITLE });
    expect(within(invested).getByText(plain)).toBeInTheDocument();
    expect(legend(invested)).toEqual([]);
  });
});

describe('HoldingsCheck: the history is older than the balances (R9)', () => {
  /**
   * The full portfolio and its comparison, as both endpoints serve them after a recompute
   * that failed: the snapshot on screen is the one before it, and `assets` is still answered.
   */
  function openStale(overrides: Partial<ReconciliationResponse> = {}): Setup {
    return openDashboard({
      positions: investedPortfolio({ last_recompute: failedRecompute() }),
      reconciliation: investedPortfolioGaps({ last_recompute: failedRecompute(), ...overrides }),
      exchanges: bothVenues(),
    });
  }

  it('says, in one alert, that the history may be older than the balances and nothing is compared', async () => {
    inTimeZone('UTC');
    openStale();

    const block = await holdingsCheck();
    const alert = within(block).getByRole('alert');
    expect(alert.tagName).toBe('P');
    expect(alert.textContent).toMatch(STALE_HISTORY);
    const time = alert.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe(RECOMPUTE_FAILED_AT);
    expect(time?.textContent).toMatch(/^Sep 24, 2026, 11:50\sAM$/);
    expect(alert.textContent).toBe(
      `The last recompute of the history failed on ${time?.textContent ?? ''}, so the history ` +
        'may be older than the balances and nothing is compared.',
    );
    // R10: "may be", not "is" - the alert does not state as fact what it cannot know.
    expect(alert).not.toHaveTextContent(/history is older/);
    // An instant that does not tick, inside a live region.
    expect(alert).not.toHaveTextContent(/ago|just now/);
  });

  it('shows neither list, neither quiet line and no figure, though the comparison was served', async () => {
    const { accounting } = openStale();

    const block = await holdingsCheck();
    await within(block).findByRole('alert');
    // Three assets short and one over are in the response: an asset bought since the snapshot
    // would look exactly like them, so none of it is put on screen.
    expect(accounting.reconciliation().assets.map((entry) => entry.status)).toEqual([
      'history_short',
      'history_short',
      'match',
      'history_over',
      'history_short',
    ]);
    expect(within(block).queryByRole('table')).toBeNull();
    expect(within(block).queryByRole('heading', { level: 4 })).toBeNull();
    expect(block.querySelector('details')).toBeNull();
    expect(block.querySelector('data')).toBeNull();
    expect(block).not.toHaveTextContent(ALL_MATCH);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
    expect(block).not.toHaveTextContent(/opening balance|Held exceeds history|History above/);
  });

  it('draws no badge, names no marker in the closed line and explains none in the legend', async () => {
    openStale();

    const block = await holdingsCheck();
    await within(block).findByRole('alert');
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');
    expect(await positionHeader('ETH')).toHaveAccessibleName(
      'ETH History incomplete Not in totals',
    );
    const invested = await investedRegion();
    expect(within(invested).queryByRole('link', { name: BADGE })).toBeNull();
    // XRP is closed and an exchange holds 12.5 of it: named without the marker.
    expect(
      within(invested).getByText(
        '2 assets no longer held are not listed: BGB (History incomplete), XRP. ' +
          'Their realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
    expect(legend(invested).map((entry) => entry.badge)).toEqual([
      'History incomplete',
      'Fee not valued',
      'Unknown cost',
    ]);
    expect(invested).not.toHaveTextContent(BADGE);
  });

  it('still says what is compared, which sources are left out, and how old the compared ones are', async () => {
    openDashboard({
      positions: investedPortfolio({ last_recompute: failedRecompute() }),
      reconciliation: investedPortfolioGaps({
        last_recompute: failedRecompute(),
        exchanges: [syncFailedBalances('bingx'), exchangeBalances()],
        wallets: walletReadings(3, { stale: 1 }),
      }),
      exchanges: [erroredExchange('unavailable', { exchange_key: 'bingx' }), exchange()],
    });

    const block = await holdingsCheck();
    expect(within(block).getByText(COMPARISON)).toBeInTheDocument();
    const alerts = within(block).getAllByRole('alert');
    expect(alerts).toHaveLength(3);
    expect(alerts[0]).toHaveTextContent(/^The last sync of BingX failed/);
    expect(alerts[1]).toHaveTextContent(/^1 wallet was last read more than 24 hours ago/);
    // The sources first, then why nothing is compared.
    expect(alerts[2]?.textContent).toMatch(STALE_HISTORY);
    expect(readings(block)).toEqual([
      { text: 'Bitget: 14 minutes ago', at: BALANCES_READ_AT },
      { text: 'Wallets (oldest reading): 20 minutes ago', at: WALLETS_OBSERVED_AT },
    ]);
  });

  it('replaces "every quantity matches" too: a match against a stale history is not one', async () => {
    const { accounting } = openDashboard({
      positions: btcOnly({ last_recompute: failedRecompute() }),
    });

    const block = await holdingsCheck();
    expect(accounting.reconciliation().assets.map((entry) => entry.status)).toEqual(['match']);
    expect(within(block).getByRole('alert').textContent).toMatch(STALE_HISTORY);
    expect(block).not.toHaveTextContent(ALL_MATCH);
  });

  it('replaces "there is nothing to compare" too', async () => {
    const { accounting } = openDashboard({
      positions: emptySnapshot({ last_recompute: failedRecompute() }),
    });

    const block = await holdingsCheck();
    expect(accounting.reconciliation().assets).toEqual([]);
    expect(within(block).getByRole('alert').textContent).toMatch(STALE_HISTORY);
    expect(block).not.toHaveTextContent(NOTHING_TO_COMPARE);
  });

  it('renders nothing when the only recompute failed and left no snapshot', async () => {
    const { accounting } = openDashboard({ positions: failedFirstRecompute() });

    const invested = await investedRegion();
    await within(invested).findByRole('heading', { name: 'Positions could not be computed' });
    await reconciliationRead(accounting);
    expect(accounting.reconciliation().last_recompute?.outcome).toBe('failed');
    // The section says it; the block has no snapshot to say anything about.
    expect(queryHoldingsCheck()).toBeNull();
    expect(invested).not.toHaveTextContent(/older than the balances/);
    expect(within(invested).getAllByRole('alert')).toHaveLength(1);
  });

  it.each(ALL_RECOMPUTE_OUTCOMES.filter((outcome) => outcome !== 'failed'))(
    'compares as usual after a recompute that was %s',
    async (outcome) => {
      openDashboard({
        positions: investedPortfolio({ last_recompute: lastRecompute({ outcome }) }),
        reconciliation: investedPortfolioGaps({ last_recompute: lastRecompute({ outcome }) }),
        exchanges: bothVenues(),
      });

      const block = await holdingsCheck();
      expect(shortTable(block, 3)).toBeInTheDocument();
      expect(within(block).queryByRole('alert')).toBeNull();
      expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    },
  );

  it('compares as usual when no recompute has been attempted since the process started', async () => {
    // `last_recompute` lives in memory: after a restart it is null over a snapshot that exists.
    openDashboard({
      positions: investedPortfolio({ last_recompute: null }),
      reconciliation: investedPortfolioGaps({ last_recompute: null }),
      exchanges: bothVenues(),
    });

    const block = await holdingsCheck();
    expect(shortTable(block, 3)).toBeInTheDocument();
    expect(within(block).queryByRole('alert')).toBeNull();
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
  });

  it('brings the comparison and the badge back once a recompute succeeds', async () => {
    fakePolling();
    const { accounting } = openStale();
    const block = await holdingsCheck();
    await within(block).findByRole('alert');
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');

    accounting.setPositions(investedPortfolio(), investedPortfolioGaps());
    nextPoll();

    await waitFor(() => {
      expect(within(block).queryByRole('alert')).not.toBeInTheDocument();
    });
    expect(shortTable(block, 3)).toBeInTheDocument();
    await waitFor(async () => {
      expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    });
  });
});

describe('HoldingsCheck: its requests', () => {
  it('reads one endpoint for the block and the badge together, with no query string', async () => {
    const { accounting } = openDashboard({ reconciliation: btcShort() });

    await holdingsCheck();
    expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    await settle();

    const requests = accounting.requestsTo('reconciliation');
    expect(requests.length).toBeGreaterThan(0);
    for (const request of requests) {
      expect(request.method).toBe('GET');
      expect(new URL(request.url).pathname).toBe('/api/accounting/reconciliation');
      expect(new URL(request.url).search).toBe('');
    }
    // The block and the positions table read one query, so the table mounting later adds no
    // request of its own: the reconciliation is asked for exactly as often as the positions,
    // whose only reader mounts with the section. (`StrictMode` mounts each reader twice and
    // the first mount's request is cancelled, so the count on its own is not "one".)
    expect(accounting.requestsTo('positions').length).toBeGreaterThan(0);
    expect(requests).toHaveLength(accounting.requestsTo('positions').length);
    expect(
      accounting.requestsTo('positions').map((entry) => new URL(entry.url).pathname),
    ).toContain(POSITIONS_PATH);
  });

  it('polls once a minute while the dashboard is open', async () => {
    fakePolling();
    const { accounting } = openDashboard();
    await holdingsCheck();
    await settle();
    const before = accounting.count('reconciliation');

    act(() => {
      vi.advanceTimersByTime(59_999);
    });
    await settle();
    expect(accounting.count('reconciliation')).toBe(before);

    act(() => {
      vi.advanceTimersByTime(1);
    });
    // Not `waitFor`: the answer is the same comparison, so nothing in the DOM changes to
    // wake it, and with `setInterval` faked it has no timer of its own.
    await settle();
    expect(accounting.count('reconciliation')).toBe(before + 1);
  });
});

describe('HoldingsCheck: after an exchange sync', () => {
  it('shows the comparison the sync left as soon as the owner returns to the details page', async () => {
    // Witness for the `['accounting']` invalidation reaching the reconciliation. The clock is
    // frozen, so the comparison cached before the sync is still fresh by `staleTime` on
    // return, and without the invalidation it would be served as it was: everything matches.
    let accounting: FakeAccounting | undefined;
    const { user } = openDashboard({
      before: (fakes) => {
        accounting = fakes.accounting;
      },
      onExchangeSync: (fake) => {
        // The run stores no fill - the snapshot does not move - and reads the balances.
        accounting?.setReconciliation(btcShort());
        const run = finishedRun({
          run_id: 9,
          trigger: 'manual',
          started_at: '2026-09-24T11:59:00.000000Z',
          accounts: [accountSucceeded('bitget')],
        });
        fake.setRuns([run]);
        return syncTriggered(run, false);
      },
    });
    const block = await holdingsCheck();
    expect(within(block).getByText(ALL_MATCH)).toBeInTheDocument();
    expect(await positionHeader('BTC')).toHaveAccessibleName('BTC');

    const nav = screen.getByRole('navigation', { name: 'Main' });
    await user.click(within(nav).getByRole('link', { name: 'Exchanges' }));
    await user.click(await screen.findByRole('button', { name: 'Sync now' }));
    await screen.findByText(/^The sync /);

    await user.click(within(nav).getByRole('link', { name: 'Details' }));

    await waitFor(async () => {
      expect(await positionHeader('BTC')).toHaveAccessibleName(`BTC ${BADGE}`);
    });
    expect(readTable(shortTable(await holdingsCheck(), 1)).rows[0]?.values).toEqual([
      BTC_BEYOND.history,
      BTC_BEYOND.wallets,
      BTC_BEYOND.exchanges,
      BTC_BEYOND.difference,
    ]);
  });
});
