import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FAST_POLL_MS, SLOW_POLL_MS } from '@/api/exchanges';
import {
  COMPLETENESS_UNKNOWN,
  DERIVED_LEGEND,
  DERIVED_MARKER,
  FEES_LEGEND,
  FILL_SIDE_LABELS,
  INVERTED_RANGE_MESSAGE,
  NO_FEE,
  NO_ORDER_ID,
  NOT_IN_USDT,
} from '@/lib/fills';
import {
  accountFailed,
  accountSucceeded,
  authFailedExchange,
  erroredExchange,
  exchange,
  finishedRun,
  interruptedExchangeRun,
  NOW,
  RECENT_REQUESTED_SINCE,
  runningExchangeRun,
  syncTriggered,
  truncatedExchange,
  TRUNCATED_EFFECTIVE_SINCE_TEXT,
  unsyncedExchange,
  type ExchangeResponse,
  type ExchangeSyncRunResponse,
} from '@/test/exchangeFixtures';
import {
  EXCHANGES_PATH,
  fakeExchanges,
  type FakeExchanges,
  type FakeExchangesOptions,
} from '@/test/fakeExchanges';
import { at18, fill, manyFills, marchFills, type ExchangeFill } from '@/test/fillFixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The transactions view on `/exchanges` (spec 024, issue #93, frontend criteria).
 *
 * Every response comes from `fakeExchanges`, which serves `GET /api/exchanges/fills` from a
 * set of rows the backend could have stored and **derives** the totals from them, so the
 * totals a test sees always agree with the rows (see `fillFixtures.ts`). The fake holds the
 * list to the rows too: a venue's `fills_stored` is the number of its rows.
 *
 * The figures asserted below are written out by hand from the March scenario's table in
 * `fillFixtures.ts`. The page's sentences come from the exported constants of
 * `lib/fills.ts` where they exist; the ones built from data (the scope, the completeness
 * notes, the pagination words) are written out, because what is under test is how they are
 * built.
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

async function advance(ms: number): Promise<void> {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
  await settle();
}

/*
 * Scenario.
 */

/** The March rows' venues, each holding exactly its own rows: Bitget 3, BingX 2. */
function marchVenues(overrides: Partial<ExchangeResponse> = {}): ExchangeResponse[] {
  return [
    exchange({ exchange_key: 'bingx', fills_stored: 2, ...overrides }),
    exchange({ fills_stored: 3, ...overrides }),
  ];
}

function marchScenario(): FakeExchangesOptions {
  return { exchanges: marchVenues(), runs: [finishedRun()], fills: marchFills() };
}

/** Both venues holding `bitget` and `bingx` rows as given, and nothing else. */
function venuesFor(fills: readonly ExchangeFill[]): ExchangeResponse[] {
  const count = (key: string) => fills.filter((row) => row.exchange_key === key).length;
  return [
    exchange({ exchange_key: 'bingx', fills_stored: count('bingx') }),
    exchange({ fills_stored: count('bitget') }),
  ];
}

interface Setup {
  readonly user: UserEvent;
  readonly fake: FakeExchanges;
}

/** A probe beside the app, standing in for the browser's back and forward buttons. */
function HistoryButtons() {
  const navigate = useNavigate();
  return (
    <>
      <button
        type="button"
        onClick={() => {
          void navigate(-1);
        }}
      >
        Browser back
      </button>
      <button
        type="button"
        onClick={() => {
          void navigate(1);
        }}
      >
        Browser forward
      </button>
      <button
        type="button"
        onClick={() => {
          void navigate('/health');
        }}
      >
        Leave for health
      </button>
    </>
  );
}

function openTransactions(
  options: FakeExchangesOptions = marchScenario(),
  path = '/exchanges',
  overrides: readonly HttpHandler[] = [],
): Setup {
  const user = userEvent.setup();
  const session = fakeSession({ initialUser: TEST_USERNAME });
  const fake = fakeExchanges({ session, ...options });
  server.use(...session.handlers, ...fake.handlers);
  server.use(...overrides);

  renderApp([path], <HistoryButtons />);

  return { user, fake };
}

/*
 * Locators.
 */

function sectionOf(heading: HTMLElement): HTMLElement {
  const section = heading.closest('section');
  if (section === null) {
    throw new Error(`"${heading.textContent}" does not head a section.`);
  }
  return section;
}

async function transactions(): Promise<HTMLElement> {
  return sectionOf(await screen.findByRole('heading', { level: 3, name: 'Transactions' }));
}

async function accounts(): Promise<HTMLElement> {
  return sectionOf(await screen.findByRole('heading', { level: 3, name: 'Accounts' }));
}

async function history(): Promise<HTMLElement> {
  return sectionOf(await screen.findByRole('heading', { level: 3, name: 'Sync history' }));
}

/** The fills table, once rows are on screen. */
async function fillsTable(): Promise<HTMLTableElement> {
  const region = await within(await transactions()).findByRole('region', { name: 'Fills' });
  const table = within(region).getByRole('table');
  if (!(table instanceof HTMLTableElement)) {
    throw new Error('The fills are not a table.');
  }
  return table;
}

function bodyRows(table: HTMLTableElement): HTMLTableRowElement[] {
  return Array.from(table.tBodies).flatMap((body) => Array.from(body.rows));
}

function cell(row: HTMLTableRowElement, column: string): HTMLTableCellElement {
  const headers = Array.from(row.closest('table')?.tHead?.rows[0]?.cells ?? []);
  const index = headers.findIndex((header) => header.textContent.trim() === column);
  const found = row.cells[index];
  if (index < 0 || found === undefined) {
    throw new Error(`The table has no "${column}" column.`);
  }
  return found;
}

function text(element: Element): string {
  return element.textContent.replace(/\s+/g, ' ').trim();
}

/** The row of the fill executed at `executedAt`, found by its `<time dateTime>`. */
function rowAt(table: HTMLTableElement, executedAt: string): HTMLTableRowElement {
  const row = table.querySelector(`time[datetime="${executedAt}"]`)?.closest('tr');
  if (row === null || row === undefined) {
    throw new Error(`No row for the fill executed at ${executedAt}.`);
  }
  return row;
}

/** The one `<data>` in `container`, whose `value` is the exact wire string. */
function dataIn(container: Element): HTMLDataElement {
  const found = container.querySelectorAll('data');
  const only = found[0];
  if (found.length !== 1 || only === undefined) {
    throw new Error(`Expected one data element, found ${String(found.length)}.`);
  }
  return only;
}

async function totals(): Promise<HTMLElement> {
  return sectionOf(
    await within(await transactions()).findByRole('heading', { level: 4, name: 'Totals' }),
  );
}

function regionTable(container: HTMLElement, name: string): HTMLTableElement {
  const table = within(within(container).getByRole('region', { name })).getByRole('table');
  if (!(table instanceof HTMLTableElement)) {
    throw new Error(`"${name}" is not a table.`);
  }
  return table;
}

/** The asset a row header names: its text up to the first character that is not a ticker's. */
function assetOf(header: Element): string {
  return /^[A-Z0-9]+/.exec(text(header))?.[0] ?? '';
}

/** The row whose row header names `name`: an asset, or a quote asset. */
function rowNamed(table: HTMLTableElement, name: string): HTMLTableRowElement {
  const row = bodyRows(table).find((candidate) => {
    const header = candidate.querySelector('th');
    return header !== null && assetOf(header) === name;
  });
  if (row === undefined) {
    throw new Error(`No row for ${name}.`);
  }
  return row;
}

/** The `role="alert"` element `element` sits in, which must exist. */
function alertAround(element: HTMLElement): HTMLElement {
  const alert = element.closest<HTMLElement>('[role="alert"]');
  if (alert === null) {
    throw new Error(`"${element.textContent}" is not inside an alert.`);
  }
  return alert;
}

/** The block a heading heads, when it is not a section: the heading's parent. */
function blockOf(heading: HTMLElement): HTMLElement {
  const block = heading.parentElement;
  if (block === null) {
    throw new Error(`"${heading.textContent}" heads nothing.`);
  }
  return block;
}

/** The `<dd>` beside the `<dt>` named `term`. */
function summaryValue(container: HTMLElement, term: string): HTMLElement {
  const dt = within(container).getByText(term, { selector: 'dt' });
  const dd = dt.nextElementSibling;
  if (!(dd instanceof HTMLElement) || dd.tagName !== 'DD') {
    throw new Error(`"${term}" has no value beside it.`);
  }
  return dd;
}

function pagination(section: HTMLElement): HTMLElement {
  return within(section).getByRole('navigation', { name: 'Pagination' });
}

function filtersGroup(section: HTMLElement): HTMLElement {
  return within(section).getByRole('group', { name: 'Transaction filters' });
}

function dateInput(section: HTMLElement, label: 'From' | 'To'): HTMLInputElement {
  const input = within(filtersGroup(section)).getByLabelText(label);
  if (!(input instanceof HTMLInputElement)) {
    throw new Error(`${label} is not an input.`);
  }
  return input;
}

/** Sets a date input as a person picking a day does: one change to a full date. */
function pickDay(input: HTMLInputElement, day: string): void {
  fireEvent.change(input, { target: { value: day } });
}

function formClearButton(section: HTMLElement): HTMLElement {
  return within(filtersGroup(section)).getByRole('button', { name: 'Clear filters' });
}

function expectHeld(button: HTMLElement): void {
  expect(button).toHaveAttribute('aria-disabled', 'true');
  expect(button).not.toHaveAttribute('disabled');
}

function expectPressable(button: HTMLElement): void {
  expect(button).not.toHaveAttribute('aria-disabled', 'true');
  expect(button).not.toHaveAttribute('disabled');
}

/** The completeness note, which must be on screen. */
function completeness(section: HTMLElement): HTMLElement {
  return within(section).getByRole('note', { name: 'Completeness' });
}

function completenessLines(section: HTMLElement): string[] {
  return within(completeness(section))
    .getAllByRole('listitem')
    .map((item) => text(item));
}

/** The last fills query the page sent. */
function lastFillQuery(fake: FakeExchanges): URLSearchParams {
  const queries = fake.fillQueries();
  const last = queries.at(-1);
  if (last === undefined) {
    throw new Error('The page sent no fills request.');
  }
  return last;
}

function ids(table: HTMLTableElement): string[] {
  return bodyRows(table).map((row) => text(cell(row, 'Order id')));
}

/** Expects `first` to come before `second` in the document. */
function expectBefore(first: Element, second: Element): void {
  expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
}

/*
 * Criterion: the section's place on the page.
 */

describe('Transactions: its place on the page', () => {
  it('sits under the toolbar and above Accounts, with Sync history below Accounts', async () => {
    const { fake } = openTransactions({
      ...marchScenario(),
      exchanges: [
        erroredExchange('unavailable', { exchange_key: 'bingx', fills_stored: 2 }),
        exchange({ fills_stored: 3 }),
      ],
    });

    const section = await transactions();
    const accountsSection = await accounts();
    const historySection = await history();
    const syncNow = screen.getByRole('button', { name: 'Sync now' });
    const alertLink = screen.getByRole('link', { name: 'See the BingX account' });

    expectBefore(syncNow, alertLink);
    expectBefore(alertLink, section);
    expectBefore(section, accountsSection);
    expectBefore(accountsSection, historySection);
    // Siblings, not nested.
    expect(section.contains(accountsSection)).toBe(false);
    expect(accountsSection.contains(historySection)).toBe(false);
    expect(fake.count('fills')).toBeGreaterThan(0);
  });

  it('does not render, nor ask for fills, when no exchange is connected', async () => {
    const { fake } = openTransactions({ exchanges: [], runs: [], fills: [] });

    await screen.findByRole('heading', { name: 'No exchange connected' });
    await settle();

    expect(screen.queryByRole('heading', { name: 'Transactions' })).not.toBeInTheDocument();
    expect(fake.count('fills')).toBe(0);
  });
});

/*
 * Criterion: rows. Side as a word, the derived marker, "none" for an order id.
 */

describe('Transactions: rows', () => {
  it('lists one row per fill, newest first', async () => {
    openTransactions();

    const table = await fillsTable();

    expect(ids(table)).toEqual(['5005', '7004', '7003', NO_ORDER_ID, '5001']);
    for (const header of [
      'When',
      'Exchange',
      'Pair',
      'Side',
      'Quantity',
      'Price',
      'Quote value',
      'USDT value',
      'Fee',
      'Order id',
    ]) {
      expect(within(table).getByRole('columnheader', { name: header })).toBeInTheDocument();
    }
  });

  it('shows a USDT-quoted buy as stored, every amount exact in its data value', async () => {
    openTransactions();
    const row = rowAt(await fillsTable(), '2026-03-02T09:15:00Z');

    expect(text(cell(row, 'Exchange'))).toBe('Bitget');
    expect(text(cell(row, 'Pair'))).toBe('BTC/USDT');
    expect(text(cell(row, 'Side'))).toBe(FILL_SIDE_LABELS.buy);
    expect(text(cell(row, 'Quantity'))).toBe('0.5 BTC');
    expect(dataIn(cell(row, 'Quantity'))).toHaveAttribute('value', '0.500000000000000000');
    expect(text(cell(row, 'Price'))).toBe('60,000.00 USDT');
    expect(dataIn(cell(row, 'Price'))).toHaveAttribute('value', '60000.000000000000000000');
    expect(text(cell(row, 'Quote value'))).toBe('30,000.00 USDT');
    expect(dataIn(cell(row, 'Quote value'))).toHaveAttribute('value', '30000.000000000000000000');
    // The column names its unit; the cell is the bare amount.
    expect(text(cell(row, 'USDT value'))).toBe('30,000.00');
    expect(dataIn(cell(row, 'USDT value'))).toHaveAttribute('value', '30000.000000000000000000');
    expect(text(cell(row, 'Fee'))).toBe('0.0005 BTC');
    expect(dataIn(cell(row, 'Fee'))).toHaveAttribute('value', '0.000500000000000000');
    expect(text(cell(row, 'Order id'))).toBe('5001');
  });

  it('writes the side as a word, whichever way the trade went', async () => {
    openTransactions();
    const table = await fillsTable();

    const sell = rowAt(table, '2026-03-10T14:00:00Z');
    const buy = rowAt(table, '2026-03-02T09:15:00Z');

    expect(text(cell(sell, 'Side'))).toBe('Sell');
    expect(text(cell(buy, 'Side'))).toBe('Buy');
    // Not colour alone: the word is the cell's whole content, and nothing but it.
    expect(cell(sell, 'Side').children).toHaveLength(0);
  });

  it('says "none" for a fill with no order id, never an empty cell', async () => {
    openTransactions();
    const row = rowAt(await fillsTable(), '2026-03-10T14:00:00Z');

    const orderId = cell(row, 'Order id');
    expect(text(orderId)).toBe(NO_ORDER_ID);
    expect(NO_ORDER_ID).toBe('none');
    expect(text(orderId)).not.toBe('');
  });

  it('marks a derived quote value and explains the mark under the table', async () => {
    openTransactions();
    const table = await fillsTable();

    const derived = rowAt(table, '2026-03-15T08:00:00Z');
    const reported = rowAt(table, '2026-03-20T20:30:00.250000Z');

    expect(text(cell(derived, 'Quote value'))).toBe(`6,000.00 USDC ${DERIVED_MARKER}`);
    expect(dataIn(cell(derived, 'Quote value'))).toHaveAttribute('value', at18('6000'));
    expect(cell(reported, 'Quote value')).not.toHaveTextContent(DERIVED_MARKER);
    // Quoted in USDC, it has no USDT value to mark.
    expect(text(cell(derived, 'USDT value'))).toBe(NOT_IN_USDT);
    expect(cell(reported, 'USDT value')).not.toHaveTextContent(DERIVED_MARKER);
    expect(within(await transactions()).getByText(DERIVED_LEGEND)).toBeInTheDocument();
  });

  it('marks a derived USDT value too: it is the same derived figure (R5, N2)', async () => {
    // A USDT fill's USDT value is its quote value, so when the venue did not report that,
    // the USDT column is as derived as the quote column and must say so.
    const rows = [
      fill({
        id: 1,
        executed_at: '2026-03-01T00:00:00Z',
        quote_quantity_derived: true,
        order_id: '4001',
      }),
      fill({ id: 2, executed_at: '2026-03-02T00:00:00Z', order_id: '4002' }),
    ];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const table = await fillsTable();

    const derived = rowAt(table, '2026-03-01T00:00:00Z');
    expect(text(cell(derived, 'Quote value'))).toBe(`30,000.00 USDT ${DERIVED_MARKER}`);
    expect(text(cell(derived, 'USDT value'))).toBe(`30,000.00 ${DERIVED_MARKER}`);
    expect(dataIn(cell(derived, 'USDT value'))).toHaveAttribute('value', at18('30000'));

    const reported = rowAt(table, '2026-03-02T00:00:00Z');
    expect(text(cell(reported, 'USDT value'))).toBe('30,000.00');
    expect(within(await transactions()).getByText(DERIVED_LEGEND)).toBeInTheDocument();
  });

  it('explains no mark when no row on the page is derived', async () => {
    const rows = marchFills().filter((row) => !row.quote_quantity_derived);
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    await fillsTable();

    expect(screen.queryByText(DERIVED_LEGEND)).not.toBeInTheDocument();
    expect(screen.queryByText(DERIVED_MARKER)).not.toBeInTheDocument();
  });

  it('gives a fill quoted in another asset no USDT value, and keeps its quote in that asset', async () => {
    openTransactions();
    const table = await fillsTable();

    const solBtc = rowAt(table, '2026-03-25T11:00:00Z');
    expect(text(cell(solBtc, 'Pair'))).toBe('SOL/BTC');
    expect(text(cell(solBtc, 'Quote value'))).toBe('0.025 BTC');
    expect(dataIn(cell(solBtc, 'Quote value'))).toHaveAttribute('value', '0.025000000000000000');
    expect(text(cell(solBtc, 'USDT value'))).toBe(NOT_IN_USDT);
    expect(cell(solBtc, 'USDT value').querySelector('data')).toBeNull();
  });

  it('shows a rebate with its sign, and a zero fee with no asset as none', async () => {
    const rows = [
      ...marchFills(),
      fill({ id: 106, executed_at: '2026-03-26T00:00:00Z', fee_asset: null, order_id: '5006' }),
    ];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const table = await fillsTable();

    const rebate = rowAt(table, '2026-03-15T08:00:00Z');
    expect(text(cell(rebate, 'Fee'))).toBe('-0.3 USDC');
    expect(dataIn(cell(rebate, 'Fee'))).toHaveAttribute('value', '-0.300000000000000000');

    const free = rowAt(table, '2026-03-26T00:00:00Z');
    expect(text(cell(free, 'Fee'))).toBe(NO_FEE);
    expect(cell(free, 'Fee').querySelector('data')).toBeNull();
  });

  it('keeps every one of 18 places in the data value, whatever the text rounds to', async () => {
    const rows = [
      fill({
        id: 1,
        executed_at: '2026-03-01T00:00:00Z',
        quantity: '0.123456789012345678',
        price: '1.5',
        quote_quantity: '0.185185183518518517',
        fee_asset: null,
      }),
    ];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const row = rowAt(await fillsTable(), '2026-03-01T00:00:00Z');

    expect(dataIn(cell(row, 'Quantity'))).toHaveAttribute('value', '0.123456789012345678');
    // The text rounds to 8 places; the attribute never does.
    expect(text(cell(row, 'Quantity'))).toBe('0.12345679 BTC');
    expect(dataIn(cell(row, 'Quote value'))).toHaveAttribute('value', '0.185185183518518517');
    expect(dataIn(cell(row, 'Price'))).toHaveAttribute('value', '1.500000000000000000');
  });

  it('times each row with its own instant', async () => {
    inTimeZone('Europe/Madrid');
    openTransactions();
    const row = rowAt(await fillsTable(), '2026-03-20T20:30:00.250000Z');

    const when = within(cell(row, 'When')).getByText(/2026/);
    expect(when.tagName).toBe('TIME');
    expect(when).toHaveAttribute('dateTime', '2026-03-20T20:30:00.250000Z');
    // Local time: 20:30 UTC is 21:30 in Madrid in March (CET).
    expect(text(when)).toBe('Mar 20, 2026, 9:30 PM');
  });

  it('scrolls the table inside a labelled, focusable region', async () => {
    openTransactions();
    const table = await fillsTable();

    const region = within(await transactions()).getByRole('region', { name: 'Fills' });
    expect(region).toContainElement(table);
    expect(region).toHaveAttribute('tabindex', '0');
  });
});

/*
 * Criterion: totals over the whole filtered set, a negative net with its sign, the non-USDT
 * group, and nothing summed on the client.
 */

describe('Transactions: totals', () => {
  it('states the USDT line, with the net signed', async () => {
    openTransactions();
    const section = await totals();

    expect(text(summaryValue(section, 'USDT spent'))).toBe('33,100.00 USDT');
    expect(dataIn(summaryValue(section, 'USDT spent'))).toHaveAttribute('value', at18('33100'));
    expect(text(summaryValue(section, 'USDT received'))).toBe('46,500.00 USDT');
    expect(dataIn(summaryValue(section, 'USDT received'))).toHaveAttribute('value', at18('46500'));
    // Buys minus sells: more was sold than bought, so the net is negative, and says so.
    expect(text(summaryValue(section, 'USDT net'))).toBe('-13,400.00 USDT');
    expect(dataIn(summaryValue(section, 'USDT net'))).toHaveAttribute(
      'value',
      '-13400.000000000000000000',
    );
  });

  it('lists each base asset with its fills, quantities, nets and USDT', async () => {
    openTransactions();
    const table = regionTable(await totals(), 'Per asset');

    const headers = bodyRows(table).map((row) => row.querySelector('th'));
    expect(headers.map((header) => (header === null ? '' : assetOf(header)))).toEqual([
      'BTC',
      'ETH',
      'SOL',
    ]);
    // The note is its own word after the asset, in the text and in the accessible name.
    expect(headers.map((header) => (header === null ? '' : text(header)))).toEqual([
      'BTC',
      'ETH (1 fill not in USDT)',
      'SOL (1 fill not in USDT)',
    ]);
    expect(
      within(table).getByRole('rowheader', { name: 'ETH (1 fill not in USDT)' }),
    ).toBeInTheDocument();

    const btc = rowNamed(table, 'BTC');
    expect(text(cell(btc, 'Fills'))).toBe('2');
    expect(text(cell(btc, 'Bought'))).toBe('0.5');
    expect(text(cell(btc, 'Sold'))).toBe('0.75');
    expect(text(cell(btc, 'USDT spent'))).toBe('30,000.00');
    expect(text(cell(btc, 'USDT received'))).toBe('46,500.00');
    expect(dataIn(cell(btc, 'Bought'))).toHaveAttribute('value', '0.500000000000000000');
    expect(dataIn(cell(btc, 'Sold'))).toHaveAttribute('value', '0.750000000000000000');
  });

  it('carries the sign of every net in its text: minus, plus, and none for zero', async () => {
    openTransactions();
    const table = regionTable(await totals(), 'Per asset');

    const btc = rowNamed(table, 'BTC');
    const eth = rowNamed(table, 'ETH');
    const sol = rowNamed(table, 'SOL');

    // Sold more than bought, over this range: negative, never clamped to zero.
    expect(text(cell(btc, 'Net'))).toBe('-0.25');
    expect(dataIn(cell(btc, 'Net'))).toHaveAttribute('value', '-0.250000000000000000');
    expect(text(cell(btc, 'USDT net'))).toBe('-16,500.00');
    expect(dataIn(cell(btc, 'USDT net'))).toHaveAttribute('value', '-16500.000000000000000000');
    expect(text(cell(sol, 'Net'))).toBe('-10');
    // Net buying: a plus, so the sign never rests on colour.
    expect(text(cell(eth, 'Net'))).toBe('+3');
    expect(text(cell(eth, 'USDT net'))).toBe('+3,100.00');
    // Nothing either way: no sign at all.
    expect(text(cell(sol, 'USDT net'))).toBe('0.00');
    expect(dataIn(cell(sol, 'USDT net'))).toHaveAttribute('value', '0.000000000000000000');
  });

  it("says how many of an asset's fills its USDT figures leave out", async () => {
    openTransactions();
    const table = regionTable(await totals(), 'Per asset');

    // ETH has one fill quoted in USDC: its USDT figures cover the other fill only.
    const eth = rowNamed(table, 'ETH');
    expect(text(cell(eth, 'Fills'))).toBe('2');
    expect(eth.querySelector('th')).toHaveTextContent('(1 fill not in USDT)');
    expect(text(cell(eth, 'USDT spent'))).toBe('3,100.00');
    // BTC's fills are all in USDT: no note.
    expect(rowNamed(table, 'BTC').querySelector('th')).not.toHaveTextContent('not in USDT');
  });

  it('says a count of unvalued fills in the plural', async () => {
    const rows = [
      fill({ id: 1, executed_at: '2026-03-01T00:00:00Z', quote_asset: 'USDC' }),
      fill({ id: 2, executed_at: '2026-03-02T00:00:00Z', quote_asset: 'EUR' }),
      fill({ id: 3, executed_at: '2026-03-03T00:00:00Z' }),
    ];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const table = regionTable(await totals(), 'Per asset');

    expect(rowNamed(table, 'BTC').querySelector('th')).toHaveTextContent('(2 fills not in USDT)');
  });

  it('groups the fills not valued in USDT by quote asset, in their own asset', async () => {
    openTransactions();
    const section = blockOf(
      within(await totals()).getByRole('heading', { level: 5, name: 'Not valued in USDT' }),
    );

    expect(section).toHaveTextContent(/^Not valued in USDT\s*2 fills are quoted/);
    const table = regionTable(section, 'Not valued in USDT');
    expect(bodyRows(table).map((row) => text(row.querySelector('th') ?? row))).toEqual([
      'BTC',
      'USDC',
    ]);

    // Sums in BTC are shown to eight places, not the cent: 0.025 BTC rounded to two places
    // would read 0.03, a fifth more than was received.
    const btc = rowNamed(table, 'BTC');
    expect(text(cell(btc, 'Fills'))).toBe('1');
    expect(text(cell(btc, 'Spent'))).toBe('0.00');
    expect(dataIn(cell(btc, 'Spent'))).toHaveAttribute('value', '0.000000000000000000');
    expect(text(cell(btc, 'Received'))).toBe('0.025');
    expect(dataIn(cell(btc, 'Received'))).toHaveAttribute('value', '0.025000000000000000');
    expect(text(cell(btc, 'Net'))).toBe('-0.025');
    expect(dataIn(cell(btc, 'Net'))).toHaveAttribute('value', '-0.025000000000000000');

    const usdc = rowNamed(table, 'USDC');
    expect(text(cell(usdc, 'Spent'))).toBe('6,000.00');
    expect(text(cell(usdc, 'Received'))).toBe('0.00');
    expect(text(cell(usdc, 'Net'))).toBe('+6,000.00');
  });

  it('says one unvalued fill in the singular', async () => {
    const rows = [
      fill({ id: 1, executed_at: '2026-03-01T00:00:00Z', quote_asset: 'USDC' }),
      fill({ id: 2, executed_at: '2026-03-02T00:00:00Z' }),
    ];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await totals();

    expect(section).toHaveTextContent('1 fill is quoted in something other than USDT.');
  });

  it('has no unvalued group when every fill is in USDT', async () => {
    const rows = marchFills().filter((row) => row.quote_asset === 'USDT');
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await totals();

    expect(
      within(section).queryByRole('heading', { name: 'Not valued in USDT' }),
    ).not.toBeInTheDocument();
  });

  it('sums fees per asset with their sign, never converted', async () => {
    openTransactions();
    const section = blockOf(
      within(await totals()).getByRole('heading', { level: 5, name: 'Fees' }),
    );

    expect(within(section).getByText(FEES_LEGEND)).toBeInTheDocument();
    const items = within(section).getAllByRole('listitem');
    expect(items.map((item) => text(item))).toEqual([
      '0.00051 BTC',
      '0.001 ETH',
      '-0.3 USDC',
      '12.5 USDT',
    ]);
    expect(items.map((item) => dataIn(item).getAttribute('value'))).toEqual([
      '0.000510000000000000',
      '0.001000000000000000',
      '-0.300000000000000000',
      '12.500000000000000000',
    ]);
  });

  it('has no fee list when no matching fill paid a fee', async () => {
    const rows = [fill({ id: 1, executed_at: '2026-03-01T00:00:00Z', fee_asset: null })];
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await totals();

    expect(within(section).queryByRole('heading', { name: 'Fees' })).not.toBeInTheDocument();
  });

  it('are the whole filtered set, not the page on screen: the client sums nothing', async () => {
    // 60 fills of 0.001 BTC at 60,000: 0.06 BTC and 3,600 USDT in all. The second page holds
    // five rows, worth 0.005 BTC and 300 USDT, and the totals must not move.
    const rows = manyFills(60);
    const { user } = openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await transactions();
    await fillsTable();

    const assertTotals = async () => {
      const btc = rowNamed(regionTable(await totals(), 'Per asset'), 'BTC');
      expect(text(cell(btc, 'Fills'))).toBe('60');
      expect(dataIn(cell(btc, 'Bought'))).toHaveAttribute('value', '0.060000000000000000');
      expect(text(summaryValue(await totals(), 'USDT spent'))).toBe('3,600.00 USDT');
      // The scope counts every match too, not the 5 rows on screen.
      expect(within(section).getByText('60 fills on all exchanges at any date.')).toBeTruthy();
    };

    await assertTotals();
    await user.click(within(pagination(section)).getByRole('button', { name: 'Next' }));
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 60');
    });
    expect(bodyRows(await fillsTable())).toHaveLength(5);
    await assertTotals();
  });

  it('states the scope in words: how many fills, which exchanges, which days', async () => {
    inTimeZone('Europe/Madrid');
    openTransactions(marchScenario(), '/exchanges');
    const section = await transactions();

    expect(await within(section).findByText('5 fills on all exchanges at any date.')).toBeTruthy();
  });

  it('states a filtered scope with the venues and the inclusive days', async () => {
    inTimeZone('Europe/Madrid');
    openTransactions(marchScenario(), '/exchanges?exchange=bitget&from=2026-03-01&to=2026-03-10');
    const section = await transactions();

    // 101 and 102 are Bitget's between 1 and 10 March; 105 is on the 25th.
    expect(
      await within(section).findByText(
        '2 fills on Bitget from Mar 1, 2026 to Mar 10, 2026 (inclusive).',
      ),
    ).toBeTruthy();
  });
});

/*
 * Criterion: filters, in the URL, with Clear filters.
 */

describe('Transactions: filters and the URL', () => {
  it('offers every venue, whatever the list holds, and none checked means all', async () => {
    openTransactions({
      exchanges: [exchange({ fills_stored: 0 })],
      runs: [],
      fills: [],
    });
    const group = filtersGroup(await transactions());

    // BingX has no account here, and is still a choice: the choices are every ExchangeKey.
    expect(within(group).getByRole('checkbox', { name: 'BingX' })).not.toBeChecked();
    expect(within(group).getByRole('checkbox', { name: 'Bitget' })).not.toBeChecked();
    expect(within(group).getByText('Leave all unchecked to include every exchange.')).toBeTruthy();
  });

  it('filters the rows and the totals by exchange, and puts the choice in the URL', async () => {
    const { user, fake } = openTransactions();
    const section = await transactions();
    await fillsTable();

    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bingx');
    });
    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['7004', '7003']);
    });
    expect(lastFillQuery(fake).getAll('exchange')).toEqual(['bingx']);
    const table = regionTable(await totals(), 'Per asset');
    expect(bodyRows(table)).toHaveLength(1);
    expect(text(cell(rowNamed(table, 'ETH'), 'Fills'))).toBe('2');
    expect(text(summaryValue(await totals(), 'USDT spent'))).toBe('3,100.00 USDT');
    expect(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' })).toBeChecked();
  });

  it('filters by several exchanges, which is every one here', async () => {
    const { user, fake } = openTransactions();
    const section = await transactions();
    await fillsTable();

    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'Bitget' }));
    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }));

    // The URL keeps the order they were checked in; the request, and so the query key, are
    // in one canonical order whatever it is.
    await waitFor(() => {
      expect(new URLSearchParams(currentPath().split('?')[1]).getAll('exchange').sort()).toEqual([
        'bingx',
        'bitget',
      ]);
    });
    await waitFor(() => {
      expect(lastFillQuery(fake).getAll('exchange')).toEqual(['bingx', 'bitget']);
    });
    expect(ids(await fillsTable())).toHaveLength(5);
    // Unchecking one leaves the other.
    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bitget');
    });
  });

  it('filters by days picked in the form, and puts them in the URL as days', async () => {
    inTimeZone('Europe/Madrid');
    const { fake } = openTransactions();
    const section = await transactions();
    await fillsTable();

    pickDay(dateInput(section, 'From'), '2026-03-10');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-10');
    });
    pickDay(dateInput(section, 'To'), '2026-03-20');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-10&to=2026-03-20');
    });

    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['7004', '7003', NO_ORDER_ID]);
    });
    const query = lastFillQuery(fake);
    // Madrid midnight on 10 March is 23:00 UTC on the 9th; the end is the start of the 21st.
    expect(query.get('from')).toBe('2026-03-09T23:00:00.000Z');
    expect(query.get('to')).toBe('2026-03-20T23:00:00.000Z');
    expect(dateInput(section, 'From')).toHaveValue('2026-03-10');
    expect(dateInput(section, 'To')).toHaveValue('2026-03-20');
  });

  it('restores the filters and the page from the URL, as a reload does', async () => {
    inTimeZone('Europe/Madrid');
    const rows = manyFills(60, { start: '2026-03-05T00:00:00Z' });
    const { fake } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?exchange=bitget&from=2026-03-01&to=2026-03-31&page=2',
    );
    const section = await transactions();

    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 60');
    });
    expect(within(filtersGroup(section)).getByRole('checkbox', { name: 'Bitget' })).toBeChecked();
    expect(
      within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }),
    ).not.toBeChecked();
    expect(dateInput(section, 'From')).toHaveValue('2026-03-01');
    expect(dateInput(section, 'To')).toHaveValue('2026-03-31');

    const query = lastFillQuery(fake);
    expect(query.getAll('exchange')).toEqual(['bitget']);
    expect(query.get('from')).toBe('2026-02-28T23:00:00.000Z');
    // 31 March is after the switch to CEST (UTC+2): the end is 1 April 00:00 CEST.
    expect(query.get('to')).toBe('2026-03-31T22:00:00.000Z');
    expect(query.get('limit')).toBe('5');
    expect(query.get('offset')).toBe('5');
  });

  it('ignores what the URL cannot mean, and asks for everything', async () => {
    const { fake } = openTransactions(
      marchScenario(),
      '/exchanges?exchange=kraken&from=2026-02-30&to=soon&page=-3',
    );
    await fillsTable();

    // Nothing the backend would refuse is ever sent: the fake answers a refused query 422.
    expect(lastFillQuery(fake).toString()).toBe('limit=5&offset=0');
    expect(ids(await fillsTable())).toHaveLength(5);
  });

  it('back and forward move between filters, and the rows follow', async () => {
    const { user } = openTransactions();
    const section = await transactions();
    await fillsTable();

    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }));
    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['7004', '7003']);
    });

    await user.click(screen.getByRole('button', { name: 'Browser back' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    await waitFor(async () => {
      expect(ids(await fillsTable())).toHaveLength(5);
    });
    expect(
      within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' }),
    ).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: 'Browser forward' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bingx');
    });
    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['7004', '7003']);
    });
    expect(within(filtersGroup(section)).getByRole('checkbox', { name: 'BingX' })).toBeChecked();
  });

  it('Clear filters removes all four parameters', async () => {
    const rows = manyFills(60, { start: '2026-03-05T00:00:00Z' });
    const { user, fake } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?exchange=bitget&from=2026-03-01&to=2026-03-31&page=2',
    );
    const section = await transactions();
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 60');
    });

    const clear = formClearButton(section);
    expectPressable(clear);
    await user.click(clear);

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 1 to 5 of 60');
    });
    expect(lastFillQuery(fake).toString()).toBe('limit=5&offset=0');
    expect(dateInput(section, 'From')).toHaveValue('');
    expect(dateInput(section, 'To')).toHaveValue('');
    // With nothing left to clear it is held, not removed, so focus stays on it.
    expectHeld(formClearButton(section));
    expect(formClearButton(section)).toHaveFocus();
  });

  it('Clear filters does nothing when no filter is set', async () => {
    const { user, fake } = openTransactions();
    const section = await transactions();
    await fillsTable();
    await settle();
    const requests = fake.count('fills');

    const clear = formClearButton(section);
    expectHeld(clear);
    await user.click(clear);
    await settle();

    expect(currentPath()).toBe('/exchanges');
    expect(fake.count('fills')).toBe(requests);
  });

  it('a filter changed on a later page returns to the first page', async () => {
    const rows = [...manyFills(60), ...manyFills(3, { key: 'bingx', firstId: 100 })];
    const { user, fake } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?page=2',
    );
    const section = await transactions();
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 63');
    });

    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'Bitget' }));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bitget');
    });
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 1 to 5 of 60');
    });
    expect(lastFillQuery(fake).get('offset')).toBe('0');

    // And a day picked on a later page does the same.
    await user.click(within(pagination(section)).getByRole('button', { name: 'Next' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bitget&page=2');
    });
    pickDay(dateInput(section, 'From'), '2026-09-01');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bitget&from=2026-09-01');
    });
  });

  it('refuses a range whose end is before its start, and sends nothing', async () => {
    const { fake } = openTransactions();
    const section = await transactions();
    await fillsTable();
    pickDay(dateInput(section, 'From'), '2026-03-20');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-20');
    });
    await settle();
    const requests = fake.count('fills');

    pickDay(dateInput(section, 'To'), '2026-03-10');

    const refusal = await within(filtersGroup(section)).findByRole('alert');
    expect(refusal).toHaveTextContent(INVERTED_RANGE_MESSAGE);
    expect(dateInput(section, 'To')).toHaveAttribute('aria-invalid', 'true');
    // What was typed stays, in the form and the URL.
    expect(currentPath()).toBe('/exchanges?from=2026-03-20&to=2026-03-10');
    expect(dateInput(section, 'To')).toHaveValue('2026-03-10');
    // No request, and no rows or totals from before standing as if they answered it.
    await settle();
    expect(fake.count('fills')).toBe(requests);
    expect(within(section).queryByRole('region', { name: 'Fills' })).not.toBeInTheDocument();
    expect(within(section).queryByRole('heading', { name: 'Totals' })).not.toBeInTheDocument();
  });

  it('accepts one day on both ends', async () => {
    inTimeZone('UTC');
    const { fake } = openTransactions(marchScenario(), '/exchanges?from=2026-03-10&to=2026-03-10');

    expect(ids(await fillsTable())).toEqual([NO_ORDER_ID]);
    expect(dateInput(await transactions(), 'To')).not.toHaveAttribute('aria-invalid', 'true');
    expect(lastFillQuery(fake).get('from')).toBe('2026-03-10T00:00:00.000Z');
    expect(lastFillQuery(fake).get('to')).toBe('2026-03-11T00:00:00.000Z');
  });

  it('offers days from 1970-01-01 to 9999-12-30 only (R5, N4)', async () => {
    openTransactions();
    const section = await transactions();

    for (const label of ['From', 'To'] as const) {
      expect(dateInput(section, label)).toHaveAttribute('min', '1970-01-01');
      expect(dateInput(section, label)).toHaveAttribute('max', '9999-12-30');
    }
  });

  it('ignores a day in the URL outside those bounds, and asks for everything (R5, N4)', async () => {
    const { fake } = openTransactions(marchScenario(), '/exchanges?from=1969-12-31&to=9999-12-31');
    const section = await transactions();

    expect(ids(await fillsTable())).toHaveLength(5);
    expect(lastFillQuery(fake).toString()).toBe('limit=5&offset=0');
    expect(dateInput(section, 'From')).toHaveValue('');
    expect(dateInput(section, 'To')).toHaveValue('');
  });

  it('keeps a day typed outside the bounds in the input, and sends nothing for it (R5, N4)', async () => {
    // Typing a year by keyboard passes through 0002, 0020 and 0202 on the way to 2026, and the
    // browser reports each one as a real date.
    const { fake } = openTransactions();
    const section = await transactions();
    await fillsTable();
    await settle();
    const requests = fake.count('fills');

    for (const typed of ['0002-09-01', '0202-09-01', '1969-12-31', '9999-12-31']) {
      pickDay(dateInput(section, 'From'), typed);
      await settle();
      expect(dateInput(section, 'From')).toHaveValue(typed);
      expect(currentPath()).toBe('/exchanges');
    }
    expect(fake.count('fills')).toBe(requests);

    pickDay(dateInput(section, 'From'), '2026-03-10');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-10');
    });
    expect(dateInput(section, 'From')).toHaveValue('2026-03-10');
    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['5005', '7004', '7003', NO_ORDER_ID]);
    });

    // Emptying the input removes the day.
    pickDay(dateInput(section, 'From'), '');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    expect(dateInput(section, 'From')).toHaveValue('');
  });

  it('a held draft gives way to the URL when another control changes the filters (R5, N4)', async () => {
    const { user } = openTransactions(marchScenario(), '/exchanges?from=2026-03-01');
    const section = await transactions();
    await fillsTable();

    pickDay(dateInput(section, 'From'), '0002-09-01');
    await settle();
    expect(dateInput(section, 'From')).toHaveValue('0002-09-01');

    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'Bitget' }));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?exchange=bitget&from=2026-03-01');
    });
    expect(dateInput(section, 'From')).toHaveValue('2026-03-01');
  });

  it('Clear filters empties a held draft too (R5, N4)', async () => {
    const { user } = openTransactions(marchScenario(), '/exchanges?exchange=bitget');
    const section = await transactions();
    await fillsTable();

    pickDay(dateInput(section, 'To'), '0020-01-01');
    await settle();
    expect(dateInput(section, 'To')).toHaveValue('0020-01-01');

    await user.click(formClearButton(section));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    expect(dateInput(section, 'To')).toHaveValue('');
    expect(dateInput(section, 'From')).toHaveValue('');
  });

  it('back restores the day the URL held, over a held draft (R5, N4)', async () => {
    const { user } = openTransactions(marchScenario(), '/exchanges?from=2026-03-01');
    const section = await transactions();
    await fillsTable();

    pickDay(dateInput(section, 'From'), '2026-03-10');
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-10');
    });
    pickDay(dateInput(section, 'From'), '0002-01-01');
    await settle();

    await user.click(screen.getByRole('button', { name: 'Browser back' }));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?from=2026-03-01');
    });
    expect(dateInput(section, 'From')).toHaveValue('2026-03-01');
    await waitFor(async () => {
      expect(ids(await fillsTable())).toHaveLength(5);
    });
  });

  it('sends the bounds themselves as instants the API can hold (R5, N4)', async () => {
    inTimeZone('UTC');
    const { fake } = openTransactions(marchScenario(), '/exchanges?from=1970-01-01&to=9999-12-30');

    expect(ids(await fillsTable())).toHaveLength(5);
    expect(lastFillQuery(fake).get('from')).toBe('1970-01-01T00:00:00.000Z');
    expect(lastFillQuery(fake).get('to')).toBe('9999-12-31T00:00:00.000Z');
  });
});

/*
 * Criterion: a picked day is the browser's local day, and the page names the zone.
 */

describe('Transactions: local days', () => {
  it('says which time zone its days are in', async () => {
    inTimeZone('Europe/Madrid');
    openTransactions();
    const section = await transactions();

    expect(within(section).getByText('Dates are days in Europe/Madrid.')).toBeTruthy();
    // Each date input is described by it.
    const note = within(section).getByText('Dates are days in Europe/Madrid.');
    expect(dateInput(section, 'From').getAttribute('aria-describedby')).toBe(note.id);
    expect(dateInput(section, 'To').getAttribute('aria-describedby')).toBe(note.id);
  });

  it('names whichever zone the browser is in', async () => {
    inTimeZone('America/New_York');
    openTransactions();

    expect(
      within(await transactions()).getByText('Dates are days in America/New_York.'),
    ).toBeTruthy();
  });

  it('a day on which the clocks go forward holds exactly its 23 hours of fills', async () => {
    // 29 March 2026 in Madrid: 00:00 CET (23:00 UTC on the 28th) to 00:00 CEST on the 30th
    // (22:00 UTC on the 29th). `midnight + 24 h` would end at 23:00 UTC and let in the first
    // hour of the 30th.
    inTimeZone('Europe/Madrid');
    const rows = [
      fill({ id: 1, executed_at: '2026-03-28T22:59:59Z', order_id: 'before' }),
      fill({ id: 2, executed_at: '2026-03-28T23:00:00Z', order_id: 'first' }),
      fill({ id: 3, executed_at: '2026-03-29T21:59:59.999000Z', order_id: 'last' }),
      fill({ id: 4, executed_at: '2026-03-29T22:00:00Z', order_id: 'next-day' }),
      fill({ id: 5, executed_at: '2026-03-29T22:30:00Z', order_id: 'next-day-2' }),
    ];
    const { fake } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?from=2026-03-29&to=2026-03-29',
    );

    expect(ids(await fillsTable())).toEqual(['last', 'first']);
    expect(lastFillQuery(fake).get('from')).toBe('2026-03-28T23:00:00.000Z');
    expect(lastFillQuery(fake).get('to')).toBe('2026-03-29T22:00:00.000Z');
  });

  it('a day on which the clocks go back holds all 25 of its hours', async () => {
    // 25 October 2026 in Madrid: 00:00 CEST (22:00 UTC on the 24th) to 00:00 CET on the
    // 26th (23:00 UTC on the 25th). `midnight + 24 h` would end at 22:00 UTC and drop the
    // day's last hour.
    inTimeZone('Europe/Madrid');
    const rows = [
      fill({ id: 1, executed_at: '2026-10-24T22:00:00Z', order_id: 'first' }),
      fill({ id: 2, executed_at: '2026-10-25T22:30:00Z', order_id: 'last-hour' }),
      fill({ id: 3, executed_at: '2026-10-25T23:00:00Z', order_id: 'next-day' }),
    ];
    const { fake } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?from=2026-10-25&to=2026-10-25',
    );

    expect(ids(await fillsTable())).toEqual(['last-hour', 'first']);
    expect(lastFillQuery(fake).get('to')).toBe('2026-10-25T23:00:00.000Z');
  });

  it('two adjacent one-day ranges share their boundary fill exactly once', async () => {
    // The API's `to` is exclusive: a fill on local midnight belongs to the day it starts.
    inTimeZone('Europe/Madrid');
    const rows = [fill({ id: 1, executed_at: '2026-03-09T23:00:00Z', order_id: 'midnight' })];
    const { user } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?from=2026-03-09&to=2026-03-09',
    );
    const section = await transactions();

    expect(
      await within(section).findByRole('heading', { name: 'Nothing matches these filters' }),
    ).toBeTruthy();

    // The next day, from the form.
    pickDay(dateInput(section, 'To'), '2026-03-10');
    pickDay(dateInput(section, 'From'), '2026-03-10');
    expect(ids(await fillsTable())).toEqual(['midnight']);
    await user.click(formClearButton(section));
    expect(ids(await fillsTable())).toEqual(['midnight']);
  });
});

/*
 * Criterion: pagination, "Showing a to b of N", `aria-disabled` at the ends.
 */

describe('Transactions: pagination', () => {
  it('pages through 12 fills 5 at a time, held at both ends', async () => {
    const rows = manyFills(12);
    const { user, fake } = openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await transactions();
    await fillsTable();
    const nav = () => pagination(section);
    const previous = () => within(nav()).getByRole('button', { name: 'Previous' });
    const next = () => within(nav()).getByRole('button', { name: 'Next' });

    expect(nav()).toHaveTextContent('Showing 1 to 5 of 12');
    expect(nav()).toHaveTextContent('Page 1 of 3');
    expect(bodyRows(await fillsTable())).toHaveLength(5);
    expectHeld(previous());
    expectPressable(next());
    // Newest first: the first page opens on the last fill.
    expect(ids(await fillsTable())[0]).toBe('1000012');

    await user.click(next());
    await waitFor(() => {
      expect(nav()).toHaveTextContent('Showing 6 to 10 of 12');
    });
    expect(currentPath()).toBe('/exchanges?page=2');
    expect(lastFillQuery(fake).get('offset')).toBe('5');
    expectPressable(previous());
    expectPressable(next());
    expect(ids(await fillsTable())[0]).toBe('100007');

    await user.click(next());
    await waitFor(() => {
      expect(nav()).toHaveTextContent('Showing 11 to 12 of 12');
    });
    expect(nav()).toHaveTextContent('Page 3 of 3');
    expect(bodyRows(await fillsTable())).toHaveLength(2);
    expectHeld(next());
    expectPressable(previous());

    // Next at the end is a no-op: no request, no navigation, and focus stays put.
    await settle();
    const requests = fake.count('fills');
    await user.click(next());
    await settle();
    expect(fake.count('fills')).toBe(requests);
    expect(currentPath()).toBe('/exchanges?page=3');
    expect(next()).toHaveFocus();

    await user.click(previous());
    await waitFor(() => {
      expect(nav()).toHaveTextContent('Showing 6 to 10 of 12');
    });
    expect(currentPath()).toBe('/exchanges?page=2');
    await user.click(previous());
    await waitFor(() => {
      expect(nav()).toHaveTextContent('Showing 1 to 5 of 12');
    });
    // Page 1 is the bare URL.
    expect(currentPath()).toBe('/exchanges');

    // Previous at the start is a no-op too.
    await settle();
    const atStart = fake.count('fills');
    await user.click(previous());
    await settle();
    expect(fake.count('fills')).toBe(atStart);
    expect(currentPath()).toBe('/exchanges');
  });

  it('one page of fills holds both buttons', async () => {
    openTransactions();
    const section = await transactions();
    await fillsTable();

    expect(pagination(section)).toHaveTextContent('Showing 1 to 5 of 5');
    expectHeld(within(pagination(section)).getByRole('button', { name: 'Previous' }));
    expectHeld(within(pagination(section)).getByRole('button', { name: 'Next' }));
  });

  it('exactly 5 fills are one page', async () => {
    const rows = manyFills(5);
    openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await transactions();
    await fillsTable();

    expect(pagination(section)).toHaveTextContent('Showing 1 to 5 of 5');
    expectHeld(within(pagination(section)).getByRole('button', { name: 'Next' }));
  });

  it('keeps the page on screen, marked busy, while the next one loads', async () => {
    const rows = manyFills(60);
    const { user, fake } = openTransactions({ exchanges: venuesFor(rows), runs: [], fills: rows });
    const section = await transactions();
    const table = await fillsTable();
    const next = within(pagination(section)).getByRole('button', { name: 'Next' });

    const release = fake.hold('fills');
    await user.click(next);
    await waitFor(() => {
      expect(fake.fillQueries().at(-1)?.get('offset')).toBe('5');
    });

    // The rows asked for under the same filters stay, marked busy, and the buttons stay
    // mounted so the keyboard keeps its place.
    expect(table).toBeInTheDocument();
    expect(table.closest('[aria-busy]')).toHaveAttribute('aria-busy', 'true');
    expect(next).toBeInTheDocument();
    expect(next).toHaveFocus();
    expect(within(section).queryByText('Loading transactions…')).not.toBeInTheDocument();

    release();
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 60');
    });
    expect((await fillsTable()).closest('[aria-busy]')).toHaveAttribute('aria-busy', 'false');
  });

  it('shows a skeleton, not the old rows, while new filters load', async () => {
    const { user, fake } = openTransactions();
    const section = await transactions();
    await fillsTable();

    const release = fake.hold('fills');
    await user.click(within(filtersGroup(section)).getByRole('checkbox', { name: 'Bitget' }));

    const skeleton = await within(section).findByText('Loading transactions…');
    expect(skeleton.closest('[role="status"]')).not.toBeNull();
    expect(within(section).queryByRole('region', { name: 'Fills' })).not.toBeInTheDocument();
    expect(within(section).queryByRole('heading', { name: 'Totals' })).not.toBeInTheDocument();

    release();
    await waitFor(async () => {
      expect(ids(await fillsTable())).toEqual(['5005', NO_ORDER_ID, '5001']);
    });
  });

  it('a page past the end offers the last page instead of nonsense', async () => {
    const rows = manyFills(10);
    const { user } = openTransactions(
      { exchanges: venuesFor(rows), runs: [], fills: rows },
      '/exchanges?page=9',
    );
    const section = await transactions();

    const back = await within(section).findByRole('button', { name: 'Go to the last page' });
    expect(section).toHaveTextContent('There is no page 9: the last page is 2.');
    expect(section).not.toHaveTextContent('Showing 41');
    expect(within(section).queryByRole('region', { name: 'Fills' })).not.toBeInTheDocument();
    // The totals are the whole set's, whatever the page.
    expect(text(cell(rowNamed(regionTable(await totals(), 'Per asset'), 'BTC'), 'Fills'))).toBe(
      '10',
    );

    await user.click(back);
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges?page=2');
    });
    await waitFor(() => {
      expect(pagination(section)).toHaveTextContent('Showing 6 to 10 of 10');
    });
  });

  it('a page past the end of one page offers page one, the bare URL', async () => {
    const { user } = openTransactions(marchScenario(), '/exchanges?page=4');
    const section = await transactions();

    const back = await within(section).findByRole('button', { name: 'Go to the last page' });
    expect(section).toHaveTextContent('There is no page 4: the last page is 1.');

    await user.click(back);
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    expect(ids(await fillsTable())).toHaveLength(5);
  });
});

/*
 * Criterion: completeness, one sentence per selected venue and reason.
 */

describe('Transactions: completeness', () => {
  it('says nothing when every selected venue holds its whole history', async () => {
    openTransactions();
    const section = await transactions();
    await fillsTable();

    expect(within(section).queryByRole('note')).not.toBeInTheDocument();
  });

  it('names a truncated venue and where its history begins', async () => {
    openTransactions({
      ...marchScenario(),
      exchanges: [
        exchange({ exchange_key: 'bingx', fills_stored: 2 }),
        truncatedExchange({ fills_stored: 3 }),
      ],
    });
    const section = await transactions();
    await fillsTable();

    expect(completenessLines(section)).toEqual([
      `Bitget history begins on ${TRUNCATED_EFFECTIVE_SINCE_TEXT}; nothing before it is held.`,
    ]);
    // Page state, not an event: not a live region, nor inside one.
    const note = completeness(section);
    expect(note.closest('[aria-live], [role="alert"], [role="status"]')).toBeNull();
    expect(note.querySelector('[aria-live], [role="alert"], [role="status"]')).toBeNull();
  });

  it('says more when the selected range starts before what is held', async () => {
    inTimeZone('UTC');
    openTransactions(
      {
        ...marchScenario(),
        exchanges: [
          exchange({ exchange_key: 'bingx', fills_stored: 2 }),
          truncatedExchange({ fills_stored: 3 }),
        ],
      },
      '/exchanges?from=2026-06-01',
    );
    const section = await transactions();
    await within(section).findByRole('heading', { name: 'Nothing matches these filters' });

    // The completeness note stands above the empty state too.
    expect(completenessLines(section)).toEqual([
      "The selected range starts before Bitget's history begins on " +
        `${TRUNCATED_EFFECTIVE_SINCE_TEXT}. Nothing before that date is held.`,
    ]);
  });

  it('names windows still to read', async () => {
    openTransactions({
      ...marchScenario(),
      exchanges: [
        exchange({ exchange_key: 'bingx', fills_stored: 2, pending_windows: 1 }),
        exchange({ fills_stored: 3, pending_windows: 3 }),
      ],
    });
    const section = await transactions();
    await fillsTable();

    expect(completenessLines(section)).toEqual([
      "BingX's import has 1 window still to read.",
      "Bitget's import has 3 windows still to read.",
    ]);
  });

  it('names a failing venue', async () => {
    openTransactions({
      ...marchScenario(),
      exchanges: [
        exchange({ exchange_key: 'bingx', fills_stored: 2 }),
        authFailedExchange('auth', { fills_stored: 3 }),
      ],
    });
    const section = await transactions();
    await fillsTable();

    // A refused key's first run left the whole history queued and the retention clamp had
    // already cut it: three reasons, one sentence each.
    expect(completenessLines(section)).toEqual([
      `Bitget history begins on ${TRUNCATED_EFFECTIVE_SINCE_TEXT}; nothing before it is held.`,
      "Bitget's import has 13 windows still to read.",
      "Bitget's last sync failed, so its latest trades may be missing.",
    ]);
  });

  it('speaks only of the venues selected', async () => {
    openTransactions(
      {
        ...marchScenario(),
        exchanges: [
          erroredExchange('unavailable', { exchange_key: 'bingx', fills_stored: 2 }),
          exchange({ fills_stored: 3 }),
        ],
      },
      '/exchanges?exchange=bitget',
    );
    const section = await transactions();
    await fillsTable();

    expect(within(section).queryByRole('note')).not.toBeInTheDocument();
  });

  it('says completeness is unknown when the exchange list could not be read', async () => {
    const { fake } = openTransactions();
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    const section = await transactions();
    await fillsTable();

    expect(completenessLines(section)).toEqual([COMPLETENESS_UNKNOWN]);
  });
});

/*
 * Criterion: three empty states, told apart, in order.
 */

describe('Transactions: empty states', () => {
  it('no fills imported yet, with nothing to clear', async () => {
    openTransactions({ exchanges: [unsyncedExchange('bitget')], runs: [], fills: [] });
    const section = await transactions();

    const heading = await within(section).findByRole('heading', {
      level: 4,
      name: 'No fills imported yet',
    });
    const state = heading.parentElement ?? section;
    expect(state).toHaveTextContent('Trades appear here once an exchange sync has imported them.');
    expect(within(state).queryByRole('button')).not.toBeInTheDocument();
    expect(state.closest('[role="alert"]')).toBeNull();
    expect(within(section).queryByRole('region', { name: 'Fills' })).not.toBeInTheDocument();
  });

  it('nothing matches these filters, with Clear filters that clears them', async () => {
    const { user } = openTransactions(marchScenario(), '/exchanges?from=2026-09-01');
    const section = await transactions();

    const heading = await within(section).findByRole('heading', {
      level: 4,
      name: 'Nothing matches these filters',
    });
    const state = heading.parentElement ?? section;
    expect(state).toHaveTextContent('No imported fill matches the selected exchanges and days.');

    await user.click(within(state).getByRole('button', { name: 'Clear filters' }));

    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    expect(ids(await fillsTable())).toHaveLength(5);
  });

  it('the last sync is failing, with a link to the account, outranks a filter', async () => {
    // A failed sync and no fills look alike and mean opposite things: the failure wins,
    // even over "nothing matches".
    openTransactions(
      {
        exchanges: [
          exchange({ exchange_key: 'bingx', fills_stored: 0 }),
          erroredExchange('unavailable', { fills_stored: 0 }),
        ],
        runs: [],
        fills: [],
      },
      '/exchanges?from=2026-09-01',
    );
    const section = await transactions();

    const heading = await within(section).findByRole('heading', {
      level: 4,
      name: 'The last sync is failing',
    });
    const state = alertAround(heading);
    expect(state).toHaveTextContent('the last sync failed for Bitget');
    const link = within(state).getByRole('link', { name: 'See the Bitget account' });
    expect(link).toHaveAttribute('href', '#exchange-bitget');
    expect(document.getElementById('exchange-bitget')).toBe(
      within(await accounts()).getByRole('listitem', { name: 'Bitget' }),
    );
    expect(
      within(section).queryByRole('heading', { name: 'Nothing matches these filters' }),
    ).not.toBeInTheDocument();
    expect(
      within(section).queryByRole('heading', { name: 'No fills imported yet' }),
    ).not.toBeInTheDocument();
  });

  it('a failing venue outranks "no fills imported yet" too, and each failing venue is linked', async () => {
    openTransactions({
      exchanges: [
        authFailedExchange('auth', { exchange_key: 'bingx' }),
        authFailedExchange('insufficient_scope'),
      ],
      runs: [],
      fills: [],
    });
    const section = await transactions();

    const heading = await within(section).findByRole('heading', {
      name: 'The last sync is failing',
    });
    const state = alertAround(heading);
    expect(state).toHaveTextContent('BingX and Bitget');
    expect(within(state).getByRole('link', { name: 'See the BingX account' })).toHaveAttribute(
      'href',
      '#exchange-bingx',
    );
    expect(within(state).getByRole('link', { name: 'See the Bitget account' })).toHaveAttribute(
      'href',
      '#exchange-bitget',
    );
  });

  it('a failing venue that is not selected does not stand in for no match', async () => {
    openTransactions(
      {
        exchanges: [
          exchange({ exchange_key: 'bingx', fills_stored: 0 }),
          erroredExchange('unavailable', { fills_stored: 0 }),
        ],
        runs: [],
        fills: [],
      },
      '/exchanges?exchange=bingx',
    );
    const section = await transactions();

    expect(
      await within(section).findByRole('heading', { name: 'Nothing matches these filters' }),
    ).toBeTruthy();
    expect(
      within(section).queryByRole('heading', { name: 'The last sync is failing' }),
    ).not.toBeInTheDocument();
  });

  it('with the list unavailable and no filter, nothing is imported yet', async () => {
    const { fake } = openTransactions({
      exchanges: [exchange({ fills_stored: 0 })],
      runs: [],
      fills: [],
    });
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    const section = await transactions();

    expect(
      await within(section).findByRole('heading', { name: 'No fills imported yet' }),
    ).toBeTruthy();
    expect(completenessLines(section)).toEqual([COMPLETENESS_UNKNOWN]);
  });

  it('with the list unavailable and a filter, nothing matches it', async () => {
    const { fake } = openTransactions(
      { exchanges: [exchange({ fills_stored: 0 })], runs: [], fills: [] },
      '/exchanges?exchange=bitget',
    );
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    const section = await transactions();

    expect(
      await within(section).findByRole('heading', { name: 'Nothing matches these filters' }),
    ).toBeTruthy();
  });

  it('an offset past the end is not an empty state: the totals still say what matched', async () => {
    openTransactions(marchScenario(), '/exchanges?page=2');
    const section = await transactions();

    await within(section).findByRole('button', { name: 'Go to the last page' });
    expect(within(section).queryByRole('heading', { name: /No fills|Nothing matches/ })).toBeNull();
  });
});

/*
 * Criterion: loading and error, each request isolated from the other.
 */

describe('Transactions: loading and failure', () => {
  it('shows a skeleton while the fills load, beside the accounts', async () => {
    const { fake } = openTransactions();
    const release = fake.hold('fills');
    const section = await transactions();

    const skeleton = await within(section).findByText('Loading transactions…');
    expect(skeleton.closest('[role="status"]')).not.toBeNull();
    expect(await within(await accounts()).findByRole('listitem', { name: 'Bitget' })).toBeTruthy();

    release();
    await fillsTable();
    expect(within(section).queryByText('Loading transactions…')).not.toBeInTheDocument();
  });

  it('a failed fills request is an error in Transactions only, with retry', async () => {
    const { user, fake } = openTransactions();
    fake.fail('fills', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    const section = await transactions();

    const alert = await within(section).findByRole('alert');
    expect(
      within(alert).getByRole('heading', { level: 4, name: 'Could not load transactions' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The database is restarting.');

    // Accounts, the toolbar and the run log are all still there.
    const bitget = await within(await accounts()).findByRole('listitem', { name: 'Bitget' });
    expect(bitget).toHaveTextContent('Up to date');
    expectPressable(screen.getByRole('button', { name: 'Sync now' }));
    expect(within(await history()).queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Could not load exchanges' })).toBeNull();

    fake.fail('fills', null);
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(ids(await fillsTable())).toHaveLength(5);
    expect(within(section).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a fills request that never reached the server says so in words', async () => {
    openTransactions(marchScenario(), '/exchanges', [
      http.get('/api/exchanges/fills', () => HttpResponse.error()),
    ]);
    const section = await transactions();

    const alert = await within(section).findByRole('alert');
    expect(alert).toHaveTextContent('Could not load transactions');
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('a failed exchange list is an error in Accounts only; Transactions still render', async () => {
    const { user, fake } = openTransactions();
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));

    const accountsSection = await accounts();
    const alert = await within(accountsSection).findByRole('alert');
    expect(
      within(alert).getByRole('heading', { level: 4, name: 'Could not load exchanges' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The database is restarting.');

    // The rows and the totals, with completeness unknown.
    const section = await transactions();
    expect(ids(await fillsTable())).toHaveLength(5);
    expect(completenessLines(section)).toEqual([COMPLETENESS_UNKNOWN]);
    expect(within(section).queryByRole('alert')).not.toBeInTheDocument();
    // No toolbar: whether any venue is configured is unknown.
    expect(screen.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
    // Nothing loaded is not nothing connected.
    expect(screen.queryByRole('heading', { name: 'No exchange connected' })).toBeNull();

    fake.fail('list', null);
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(
      await within(await accounts()).findByRole('listitem', { name: 'Bitget' }),
    ).toHaveTextContent('Up to date');
    expect(screen.getByRole('button', { name: 'Sync now' })).toBeInTheDocument();
    await waitFor(() => {
      expect(within(section).queryByRole('note')).not.toBeInTheDocument();
    });
  });

  it('a failed refresh keeps the rows on screen, with a notice', async () => {
    const { user, fake } = openTransactions();
    const section = await transactions();
    const table = await fillsTable();

    fake.fail('fills', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    await user.click(screen.getByRole('button', { name: 'Sync now' }));

    const notice = await within(section).findByRole('alert');
    expect(notice).toHaveTextContent(
      'Could not refresh transactions: The database is restarting. Showing what was last loaded.',
    );
    expect(table).toBeInTheDocument();
    expect(ids(table)).toHaveLength(5);
  });
});

/*
 * Criterion: refresh through the existing invalidation, with no polling of its own.
 */

describe('Transactions: refresh', () => {
  it('reads the fills again once a sync settles, and shows what it stored', async () => {
    const stored = fill({
      id: 201,
      exchange_key: 'bitget',
      executed_at: '2026-09-24T11:59:00Z',
      order_id: '9201',
    });
    const { user, fake } = openTransactions({
      ...marchScenario(),
      onSync: (server) => {
        server.addFills([stored]);
        return syncTriggered(
          finishedRun({
            run_id: 9,
            trigger: 'manual',
            started_at: NOW,
            accounts: [
              accountSucceeded('bingx'),
              accountSucceeded('bitget', { fills_seen: 1, fills_inserted: 1 }),
            ],
          }),
        );
      },
    });
    const section = await transactions();
    await fillsTable();
    await settle();
    const requests = fake.count('fills');

    await user.click(screen.getByRole('button', { name: 'Sync now' }));

    await waitFor(async () => {
      expect(ids(await fillsTable())[0]).toBe('9201');
    });
    expect(within(section).getByText('6 fills on all exchanges at any date.')).toBeTruthy();
    expect(text(cell(rowNamed(regionTable(await totals(), 'Per asset'), 'BTC'), 'Fills'))).toBe(
      '3',
    );
    await settle();
    // R5, S1, as the coordinator approved: the settled mutation invalidates ['exchanges'],
    // and the list it re-reads shows Bitget's fills_stored moved, which invalidates the
    // fills once more. One spare read, and no more than one.
    expect(fake.count('fills')).toBe(requests + 2);
  });

  it('reads the fills once after a sync that stored nothing', async () => {
    // The default fake sync records a run and stores no fill: the counts do not move, so
    // only the mutation's own invalidation reads the fills.
    const { user, fake } = openTransactions();
    await fillsTable();
    await settle();
    const requests = fake.count('fills');

    await user.click(screen.getByRole('button', { name: 'Sync now' }));
    await screen.findByText(/^The sync /);
    await settle();

    expect(fake.count('fills')).toBe(requests + 1);
  });

  it('reads the fills again after a failed sync request too', async () => {
    const { user, fake } = openTransactions();
    await fillsTable();
    await settle();
    const requests = fake.count('fills');
    fake.fail('sync', () => problem(502, 'Bad Gateway', 'The proxy gave up.'));

    await user.click(screen.getByRole('button', { name: 'Sync now' }));
    await screen.findByText(/The sync request failed/);
    await settle();

    expect(fake.count('fills')).toBe(requests + 1);
  });

  it('does not poll', async () => {
    fakeIntervals();
    const { fake } = openTransactions({
      ...marchScenario(),
      exchanges: marchVenues({ syncing: true }),
      runs: [runningExchangeRun({ accounts_total: 2 }), finishedRun()],
    });
    await fillsTable();
    await settle();
    const fills = fake.count('fills');
    const list = fake.count('list');

    // The list polls fast while a venue is syncing; the fills are not polled at all.
    await advance(SLOW_POLL_MS);
    await advance(SLOW_POLL_MS);

    expect(fake.count('list')).toBeGreaterThan(list);
    expect(fake.count('fills')).toBe(fills);
  });

  it('the first load reads the fills no more often than the list (R5, S1)', async () => {
    const { fake } = openTransactions();
    await fillsTable();
    await settle();

    // The list's first answer is not a change in what is stored, so it invalidates nothing.
    // Under StrictMode every query here is read twice on mount: the first mount's read is
    // aborted when React unmounts it, and the remount reads again. So the measure is the
    // list's own count, which no invalidation touches, not a literal 1.
    expect(fake.count('fills')).toBe(fake.count('list'));
    expect(fake.count('fills')).toBe(fake.count('runs'));
  });

  it('a scheduled sync that stores fills is read through the list poll, with no sync of this page (R5, S1)', async () => {
    fakeIntervals();
    const { fake } = openTransactions();
    const section = await transactions();
    await fillsTable();
    await settle();
    const before = fake.count('fills');

    // A scheduled run stores a fill: the rows and Bitget's fills_stored move together, and
    // nothing on this page asked for it.
    fake.addFills([fill({ id: 201, executed_at: '2026-09-24T11:59:00Z', order_id: '9201' })]);
    await advance(SLOW_POLL_MS);

    await waitFor(async () => {
      expect(ids(await fillsTable())[0]).toBe('9201');
    });
    expect(within(section).getByText('6 fills on all exchanges at any date.')).toBeTruthy();
    await settle();
    expect(fake.count('fills')).toBe(before + 1);
    expect(fake.count('sync')).toBe(0);

    // And the next poll, which finds the same counts, reads nothing more.
    await advance(SLOW_POLL_MS);
    expect(fake.count('fills')).toBe(before + 1);
  });

  it("a list poll that moves only a venue's windows and sync time reads no fills (R5, S1)", async () => {
    fakeIntervals();
    const { fake } = openTransactions();
    await fillsTable();
    await settle();
    const fills = fake.count('fills');
    const list = fake.count('list');

    // A run planned windows and has stored nothing yet; then it is marked synced again.
    fake.patchExchange('bingx', { pending_windows: 3 });
    await advance(SLOW_POLL_MS);
    fake.patchExchange('bingx', { pending_windows: 0, last_synced_at: NOW });
    await advance(SLOW_POLL_MS);

    expect(fake.count('list')).toBe(list + 2);
    expect(fake.count('fills')).toBe(fills);
  });

  it('coming back to the page reads nothing the cache already holds (R5, S1)', async () => {
    // The page's first reading of the counts is a baseline, not a change, even when the
    // list comes from the cache on a return visit.
    const { user, fake } = openTransactions();
    await fillsTable();
    await settle();
    const fills = fake.count('fills');

    await user.click(screen.getByRole('button', { name: 'Leave for health' }));
    await screen.findByRole('heading', { name: 'Backend health' });
    await user.click(screen.getByRole('button', { name: 'Browser back' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/exchanges');
    });
    expect(ids(await fillsTable())).toHaveLength(5);
    await settle();

    expect(fake.count('fills')).toBe(fills);
  });

  it('a list that recovers from a failed first load does not read the fills again (R5, S1)', async () => {
    const { user, fake } = openTransactions();
    fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is restarting.'));
    const alert = await within(await accounts()).findByRole('alert');
    await fillsTable();
    await settle();
    const fills = fake.count('fills');

    // Unknown counts becoming known is not a change in what is stored.
    fake.fail('list', null);
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));
    await within(await accounts()).findByRole('listitem', { name: 'Bitget' });
    await settle();

    expect(fake.count('fills')).toBe(fills);
  });

  it('while a venue is syncing, a rising count reads nothing until the run ends (R5, S1)', async () => {
    // A backfill commits a page at a time, and refreshing the whole view and its totals at
    // every 5-second poll would be polling by another name. One read, once it has ended.
    fakeIntervals();
    const { fake } = openTransactions();
    await fillsTable();
    await settle();
    const before = fake.count('fills');

    fake.patchExchange('bitget', { syncing: true });
    await advance(SLOW_POLL_MS);
    fake.addFills([fill({ id: 201, executed_at: '2026-09-24T11:58:00Z', order_id: '9201' })]);
    await advance(FAST_POLL_MS);
    fake.addFills([fill({ id: 202, executed_at: '2026-09-24T11:59:00Z', order_id: '9202' })]);
    await advance(FAST_POLL_MS);
    expect(fake.count('fills')).toBe(before);

    fake.patchExchange('bitget', { syncing: false });
    await advance(FAST_POLL_MS);

    await waitFor(async () => {
      expect(ids(await fillsTable()).slice(0, 2)).toEqual(['9202', '9201']);
    });
    await settle();
    expect(fake.count('fills')).toBe(before + 1);
  });

  it('a run that ends having stored nothing reads nothing (R5, S1)', async () => {
    fakeIntervals();
    const { fake } = openTransactions();
    await fillsTable();
    await settle();
    const before = fake.count('fills');

    fake.patchExchange('bingx', { syncing: true });
    await advance(SLOW_POLL_MS);
    fake.patchExchange('bingx', { syncing: false });
    await advance(FAST_POLL_MS);
    await advance(SLOW_POLL_MS);

    expect(fake.count('fills')).toBe(before);
  });

  it('a venue appearing with fills of its own is a change in what is stored (R5, S1)', async () => {
    fakeIntervals();
    const bitgetOnly = marchFills().filter((row) => row.exchange_key === 'bitget');
    const { fake } = openTransactions({
      exchanges: [exchange({ fills_stored: bitgetOnly.length })],
      runs: [finishedRun()],
      fills: bitgetOnly,
    });
    await fillsTable();
    await settle();
    const before = fake.count('fills');

    // BingX's credentials are configured and its first scheduled run stores two fills.
    fake.setExchanges([exchange({ exchange_key: 'bingx', fills_stored: 0 }), ...fake.exchanges()]);
    fake.addFills(marchFills().filter((row) => row.exchange_key === 'bingx'));
    await advance(SLOW_POLL_MS);

    await waitFor(async () => {
      expect(ids(await fillsTable())).toHaveLength(5);
    });
    expect(fake.count('fills')).toBe(before + 1);
  });
});

/*
 * Criterion: Sync history, collapsed unless something is wrong, the owner's toggle winning.
 */

describe('Sync history: the disclosure', () => {
  async function disclosure(): Promise<HTMLDetailsElement> {
    const section = await history();
    const details = await waitFor(() => {
      const found = section.querySelector('details');
      if (found === null) {
        throw new Error('Sync history has no disclosure.');
      }
      return found;
    });
    return details;
  }

  function summaryOf(details: HTMLDetailsElement): HTMLElement {
    const summary = details.querySelector('summary');
    if (summary === null) {
      throw new Error('The disclosure has no summary.');
    }
    return summary;
  }

  it('is collapsed when the newest run succeeded and no account is failing', async () => {
    openTransactions();
    const details = await disclosure();

    expect(details).not.toHaveAttribute('open');
    // The summary is always there, with the newest run's outcome and age.
    expect(text(summaryOf(details))).toBe('Latest run: Succeeded, started 20 minutes ago');
    expect(within(summaryOf(details)).getByText('20 minutes ago').tagName).toBe('TIME');
  });

  it.each([
    [
      'partial',
      finishedRun({
        accounts: [accountSucceeded('bingx'), accountFailed('bitget', 'unavailable')],
      }),
      'Partially succeeded',
    ],
    ['failed', finishedRun({ accounts: [accountFailed('bitget', 'unavailable')] }), 'Failed'],
    ['interrupted', interruptedExchangeRun({ accounts_total: 1 }), 'Interrupted'],
  ] as const)(
    'is open when the newest run is %s',
    async (_status, run: ExchangeSyncRunResponse, label) => {
      openTransactions({ ...marchScenario(), runs: [run, finishedRun({ run_id: 1 })] });
      const details = await disclosure();

      expect(details).toHaveAttribute('open');
      expect(summaryOf(details)).toHaveTextContent(`Latest run: ${label}, started`);
    },
  );

  it('is collapsed while the newest run is still running', async () => {
    openTransactions({
      ...marchScenario(),
      runs: [runningExchangeRun({ accounts_total: 2 }), finishedRun()],
    });
    const details = await disclosure();

    expect(details).not.toHaveAttribute('open');
    expect(summaryOf(details)).toHaveTextContent('Latest run: Running, started 1 minute ago');
  });

  it.each([
    ['error', erroredExchange('unavailable', { fills_stored: 3 })],
    ['auth_failed', authFailedExchange('auth', { fills_stored: 3 })],
  ] as const)(
    'is open when an account is %s, even under a run that succeeded',
    async (_status, failing) => {
      openTransactions({
        ...marchScenario(),
        exchanges: [exchange({ exchange_key: 'bingx', fills_stored: 2 }), failing],
      });

      expect(await disclosure()).toHaveAttribute('open');
    },
  );

  it("the owner's toggle wins over what later polls find", async () => {
    fakeIntervals();
    const { user, fake } = openTransactions();
    const details = await disclosure();
    expect(details).not.toHaveAttribute('open');

    // The owner opens it.
    await user.click(summaryOf(details));
    expect(details).toHaveAttribute('open');
    // And closes it again: their choice, not the page's.
    await user.click(summaryOf(details));
    expect(details).not.toHaveAttribute('open');

    // A failed run appears; the page would open the log on its own, but the owner closed it.
    fake.setRuns([
      finishedRun({
        run_id: 8,
        started_at: NOW,
        accounts: [accountFailed('bitget', 'unavailable')],
      }),
      finishedRun(),
    ]);
    fake.patchExchange('bitget', {
      status: 'error',
      last_error: { error_kind: 'unavailable', detail: null },
      pending_windows: 1,
    });
    await advance(SLOW_POLL_MS);
    await waitFor(() => {
      expect(summaryOf(details)).toHaveTextContent('Latest run: Failed');
    });

    expect(details).not.toHaveAttribute('open');
  });

  it("the owner's toggle wins over a log that opened itself", async () => {
    const { user } = openTransactions({
      ...marchScenario(),
      runs: [finishedRun({ accounts: [accountFailed('bitget', 'unavailable')] })],
    });
    const details = await disclosure();
    expect(details).toHaveAttribute('open');

    await user.click(summaryOf(details));
    expect(details).not.toHaveAttribute('open');
    await user.click(summaryOf(details));
    expect(details).toHaveAttribute('open');
  });

  it('opens on its own when a failure appears, until the owner has chosen', async () => {
    fakeIntervals();
    const { fake } = openTransactions();
    const details = await disclosure();
    expect(details).not.toHaveAttribute('open');

    fake.setRuns([
      finishedRun({
        run_id: 8,
        started_at: NOW,
        accounts: [accountFailed('bitget', 'unavailable')],
      }),
      finishedRun(),
    ]);
    await advance(SLOW_POLL_MS);

    await waitFor(() => {
      expect(details).toHaveAttribute('open');
    });
  });

  it('a disclosure the browser opened closes on the next click (R5, N5)', async () => {
    // Find-in-page opens a closed <details> on its own to show a match, and React is not
    // asked. The next click must act on what the owner sees, which is an open log.
    const { user } = openTransactions();
    const details = await disclosure();
    expect(details).not.toHaveAttribute('open');

    act(() => {
      details.open = true;
      details.dispatchEvent(new Event('toggle'));
    });
    await settle();
    expect(details).toHaveAttribute('open');

    await user.click(summaryOf(details));
    expect(details).not.toHaveAttribute('open');
    await settle();
    expect(details).not.toHaveAttribute('open');

    // And it opens again on the one after.
    await user.click(summaryOf(details));
    expect(details).toHaveAttribute('open');
  });

  it('a disclosure the browser closed opens on the next click (R5, N5)', async () => {
    const { user } = openTransactions({
      ...marchScenario(),
      runs: [finishedRun({ accounts: [accountFailed('bitget', 'unavailable')] })],
    });
    const details = await disclosure();
    expect(details).toHaveAttribute('open');

    act(() => {
      details.open = false;
      details.dispatchEvent(new Event('toggle'));
    });
    await settle();
    expect(details).not.toHaveAttribute('open');

    await user.click(summaryOf(details));
    expect(details).toHaveAttribute('open');
  });

  it('with no run yet there is nothing to summarise, and no disclosure', async () => {
    openTransactions({ ...marchScenario(), runs: [] });
    const section = await history();

    expect(await within(section).findByText('No exchange sync has run yet.')).toBeTruthy();
    expect(section.querySelector('details')).toBeNull();
  });
});

/*
 * Criterion: a failing account has its own line above the transactions, linking to it.
 */

describe('Failing-account alert', () => {
  it('names each failing venue above the transactions and links to its entry', async () => {
    openTransactions({
      ...marchScenario(),
      exchanges: [
        authFailedExchange('auth', { exchange_key: 'bingx', fills_stored: 2 }),
        erroredExchange('rate_limited', { fills_stored: 3 }),
      ],
    });
    const section = await transactions();
    const accountsSection = await accounts();

    for (const [key, name] of [
      ['bingx', 'BingX'],
      ['bitget', 'Bitget'],
    ] as const) {
      const link = screen
        .getAllByRole('link', { name: `See the ${name} account` })
        .find((candidate) => !section.contains(candidate) && !accountsSection.contains(candidate));
      if (link === undefined) {
        throw new Error(`No alert line links to ${name}.`);
      }
      const line = link.closest('[role="alert"]');
      expect(line).toHaveTextContent(`${name}: the last sync failed.`);
      expect(link).toHaveAttribute('href', `#exchange-${key}`);
      expectBefore(link, section);

      // The anchor lands on the account's own entry.
      const target = document.getElementById(`exchange-${key}`);
      expect(target).toBe(within(accountsSection).getByRole('listitem', { name }));
    }
  });

  it('is absent when no account is failing, and for a venue that never synced', async () => {
    openTransactions({
      exchanges: [
        unsyncedExchange('bingx'),
        exchange({ fills_stored: 3, requested_since: RECENT_REQUESTED_SINCE }),
      ],
      runs: [],
      fills: marchFills().filter((row) => row.exchange_key === 'bitget'),
    });
    await fillsTable();

    expect(screen.queryByText(/the last sync failed/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('every account entry carries its anchor', async () => {
    openTransactions();
    const accountsSection = await accounts();

    for (const name of ['BingX', 'Bitget']) {
      const item = await within(accountsSection).findByRole('listitem', { name });
      expect(item.id).toBe(`exchange-${name.toLowerCase()}`);
    }
  });
});

/*
 * The fake itself: a refused query fails the test that sends it.
 */

describe('the fills fake refuses what the backend refuses', () => {
  it.each([
    ['exchange=kraken', 'is not an ExchangeKey'],
    ['from=2026-03-01T00:00:00', 'timezone-aware'],
    ['to=20260301', 'timezone-aware'],
    ['from=2026-03-02T00:00:00Z&to=2026-03-01T00:00:00Z', 'before'],
    ['from=2026-03-01T00:00:00Z&to=2026-03-01T00:00:00Z', 'before'],
    ['limit=0', 'limit'],
    ['limit=201', 'limit'],
    ['offset=-1', 'offset'],
    ['offset=9223372036854775808', 'offset'],
    ['page=2', 'not a parameter'],
  ])('%s is a 422', async (query, reason) => {
    const fake = fakeExchanges({ exchanges: [], fills: [] });
    server.use(...fake.handlers);

    const response = await fetch(`${EXCHANGES_PATH}/fills?${query}`);
    const body = (await response.json()) as { detail: string };

    expect(response.status).toBe(422);
    expect(body.detail).toContain(reason);
  });

  it('answers a query the backend accepts, with the same totals on every page', async () => {
    const rows = manyFills(3);
    const fake = fakeExchanges({ exchanges: venuesFor(rows), fills: rows });
    server.use(...fake.handlers);

    const first = (await (
      await fetch(`${EXCHANGES_PATH}/fills?exchange=bitget&exchange=bitget&limit=2`)
    ).json()) as { fills: unknown[]; total_count: number; totals: unknown };
    const past = (await (
      await fetch(`${EXCHANGES_PATH}/fills?offset=9223372036854775807`)
    ).json()) as { fills: unknown[]; total_count: number; totals: unknown };

    expect(first.fills).toHaveLength(2);
    expect(first.total_count).toBe(3);
    expect(past.fills).toEqual([]);
    expect(past.totals).toEqual(first.totals);
  });

  it('refuses a list that disagrees with the rows', () => {
    expect(() =>
      fakeExchanges({ exchanges: [exchange({ fills_stored: 4 })], fills: marchFills() }),
    ).toThrow('fills_stored');
  });
});
