import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  EXCLUSION_REASON_MESSAGES,
  FLAG_BADGES,
  FLAG_EXPLANATIONS,
  MARKET_VALUE_UNAVAILABLE_MESSAGES,
} from '@/lib/accounting';
import {
  accountingPrice,
  bgbFeeNeverHeld,
  breakEvenPortfolio,
  COMPUTED_AT,
  emptySnapshot,
  ethUnpriced,
  everyHeldPositionExcluded,
  failedFirstRecompute,
  failedRecompute,
  FEE_OCCURRED_AT,
  feeInNeverHeldAsset,
  INVESTED_TOTALS,
  investedPortfolio,
  kasLossPortfolio,
  kasUnknownBasis,
  noSnapshot,
  position,
  positionsResponse,
  RECOMPUTE_ERROR,
  RECOMPUTE_FAILED_AT,
  stablecoinOnlySnapshot,
  tinyPnlPortfolio,
  totals,
  warning,
  WARNING_OCCURRED_AT,
  xrpClosed,
  ZERO,
  type PositionsResponse,
} from '@/test/accountingFixtures';
import {
  accountSucceeded,
  authFailedExchange,
  erroredExchange,
  exchange,
  finishedRun,
  syncTriggered,
  unsyncedExchange,
  type ExchangeResponse,
} from '@/test/exchangeFixtures';
import { fakeAccounting, POSITIONS_PATH, type FakeAccounting } from '@/test/fakeAccounting';
import {
  EXCHANGES_PATH,
  fakeExchanges,
  type FakeExchanges,
  type FakeExchangesOptions,
} from '@/test/fakeExchanges';
import {
  BALANCES_CURRENT_PATH,
  fakePortfolio,
  type FakePortfolioOptions,
} from '@/test/fakePortfolio';
import { emptyPortfolio, healthyPortfolio, HEALTHY, NOW, STALE_PRICE_AS_OF } from '@/test/fixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';

/**
 * The invested section of the dashboard (spec 022), rendered inside the whole app at `/`
 * with the value section beside it, so that every test here also proves the two sections
 * stand apart.
 *
 * Every expected figure is written out by hand, next to the fixture that produces it in
 * `test/accountingFixtures.ts`. Every `<data value>` is compared with the exact wire string,
 * 18 places and trailing zeros included.
 *
 * `Date` is faked and fixed at `NOW`; `setTimeout` stays real, because MSW answers through
 * it. A test that needs a poll fakes `setInterval` too.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

interface OpenOptions {
  readonly positions?: PositionsResponse;
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
  const accounting = fakeAccounting({ positions: options.positions ?? investedPortfolio() });
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

  renderApp(['/']);

  return { user, accounting, exchanges };
}

async function investedRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Invested' });
}

/**
 * The section once it has left its loading state. Its `h2` renders from the first frame, so
 * the region alone is found before any figure has arrived.
 */
async function loadedRegion(): Promise<HTMLElement> {
  const region = await investedRegion();
  await waitFor(() => {
    expect(within(region).queryByText('Loading invested per asset…')).not.toBeInTheDocument();
  });
  return region;
}

async function positionsTable(): Promise<HTMLTableElement> {
  const table = await within(await loadedRegion()).findByRole('table');
  if (!(table instanceof HTMLTableElement)) {
    throw new Error('The positions table is not a table.');
  }
  return table;
}

/** The table row whose row header starts with `symbol`, badges or not. */
async function positionRow(symbol: string): Promise<HTMLTableRowElement> {
  const header = within(await positionsTable()).getByRole('rowheader', {
    name: new RegExp(`^${symbol}(\\s|$)`),
  });
  const row = header.closest('tr');
  if (row === null) {
    throw new Error(`The ${symbol} row header is not inside a table row.`);
  }
  return row;
}

/** The symbols of every row, in order. */
async function rowSymbols(): Promise<string[]> {
  return within(await positionsTable())
    .getAllByRole('rowheader')
    .map((header) => header.childNodes[0]?.textContent ?? '');
}

/** The cell of `row` under the column headed exactly `column`. */
function cell(row: HTMLTableRowElement, column: string): HTMLElement {
  const table = row.closest('table');
  const headers = Array.from(table?.querySelectorAll('thead th') ?? []);
  const index = headers.findIndex((header) => header.textContent.trim() === column);
  const found = row.children.item(index);

  if (index === -1 || !(found instanceof HTMLElement)) {
    throw new Error(
      `No "${column}" column. Headers were: ${headers.map((h) => h.textContent).join(', ')}`,
    );
  }
  return found;
}

/** The exact strings every `<data value>` inside `element` carries. */
function dataValues(element: HTMLElement): (string | null)[] {
  return Array.from(element.querySelectorAll('data')).map((data) => data.getAttribute('value'));
}

/** A cell that renders "—" and no amount at all. */
function expectDash(element: HTMLElement): void {
  expect(element.textContent.trim()).toBe('—');
  expect(element.querySelector('data')).toBeNull();
}

/** The summary's `<dd>` for the `<dt>` reading exactly `term`. */
async function summaryValue(term: string): Promise<HTMLElement> {
  const region = await loadedRegion();
  const dt = (await within(region).findAllByRole('term')).find(
    (entry) => entry.textContent.trim() === term,
  );
  const dd = dt?.nextElementSibling;
  if (!(dd instanceof HTMLElement) || dd.tagName !== 'DD') {
    throw new Error(`No summary entry "${term}".`);
  }
  return dd;
}

/** The legend's badge texts, in order, and each one's explanation. */
function legend(region: HTMLElement): { badge: string; explanation: string }[] {
  const badgeTexts: readonly string[] = Object.values(FLAG_BADGES);
  return within(region)
    .queryAllByRole('term')
    .filter((dt) => badgeTexts.includes(dt.textContent.trim()))
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

/**
 * A standalone zero amount in rendered text: `0`, `0.00`, `+0`, `-0.0`, as a word of its
 * own - the same pattern the value section's tests use.
 */
const ZERO_AMOUNT = /(?:^|[^\w.,])[-+]?0(?:\.0+)?(?![\w.,%])/;

function expectNoRenderedZero(element: HTMLElement): void {
  for (const value of dataValues(element)) {
    expect(value).not.toMatch(/^-?0(?:\.0+)?$/);
  }
  expect(element.textContent).not.toMatch(ZERO_AMOUNT);
}

const COLUMNS = [
  'Asset',
  'Quantity',
  'Average cost (USD)',
  'Invested (USD)',
  'Price (USD)',
  'Market value (USD)',
  'Unrealized P&L (USD)',
  'Return',
];

/*
 * The round-2 sentences (spec 022, R8), written out: each is a statement to the owner, and a
 * change to one should be a diff here.
 */
const STALE_TOTALS = 'These totals include at least one stale price.';
const UNALLOCATED_LINE =
  'Costs not assigned to any asset: 0.10 USD, from stablecoin conversions or from swaps ' +
  'into units with no known cost.';
const NO_TRADES_NEUTRAL = 'Positions appear here once trades are imported from an exchange.';

function realizedCaveat(assets: string): string {
  return (
    `Realized P&L may be inaccurate for ${assets}: the imported history is incomplete, or a ` +
    'fee could not be valued.'
  );
}

/** BTC alone, fully comparable: the totals are its own figures. */
const BTC_ONLY_TOTALS = {
  total_invested: '52500.000000000000000000',
  market_value: '90000.000000000000000000',
  unrealized_pnl: '37500.000000000000000000',
  unrealized_return_pct: '71.4286',
  realized_pnl: '7500.000000000000000000',
} as const;

describe('InvestedSection: the table (criteria 1 and 3)', () => {
  it('lists the held assets in the endpoint order, under the columns the spec names', async () => {
    openDashboard();

    const table = await positionsTable();
    expect(
      Array.from(table.querySelectorAll('thead th')).map((header) => header.textContent.trim()),
    ).toEqual(COLUMNS);
    // XRP is no longer held: not a row.
    expect(await rowSymbols()).toEqual(['BTC', 'ETH', 'KAS', 'SOL']);
  });

  it('shows a priced, fully comparable asset figure by figure, from the exact wire strings', async () => {
    openDashboard();

    const btc = await positionRow('BTC');
    expect(btc.querySelector('th')).toHaveAccessibleName('BTC');

    expect(dataValues(cell(btc, 'Quantity'))).toEqual(['1.500000000000000000']);
    expect(cell(btc, 'Quantity').textContent.trim()).toBe('1.5');

    expect(dataValues(cell(btc, 'Average cost (USD)'))).toEqual(['35000.000000000000000000']);
    expect(cell(btc, 'Average cost (USD)').textContent.trim()).toBe('35,000.00');

    expect(dataValues(cell(btc, 'Invested (USD)'))).toEqual(['52500.000000000000000000']);
    expect(cell(btc, 'Invested (USD)').textContent.trim()).toBe('52,500.00');

    // The price at the stored scale, twelve places; fresh, so nothing about its age.
    expect(dataValues(cell(btc, 'Price (USD)'))).toEqual(['60000.000000000000']);
    expect(cell(btc, 'Price (USD)').textContent.trim()).toBe('60,000.00');
    expect(cell(btc, 'Price (USD)').querySelector('time')).toBeNull();

    expect(dataValues(cell(btc, 'Market value (USD)'))).toEqual(['90000.000000000000000000']);
    expect(cell(btc, 'Market value (USD)').textContent.trim()).toBe('90,000.00');

    expect(dataValues(cell(btc, 'Unrealized P&L (USD)'))).toEqual(['37500.000000000000000000']);
    expect(cell(btc, 'Unrealized P&L (USD)').textContent.trim()).toBe('+37,500.00');

    expect(dataValues(cell(btc, 'Return'))).toEqual(['71.4286']);
    expect(cell(btc, 'Return').textContent.trim()).toBe('+71.43%');
  });

  it('keeps every one of 18 places a double cannot hold', async () => {
    // 2.718281828459045235 as a double is 2.718281828459045: the trailing 235 would vanish.
    openDashboard();

    const eth = await positionRow('ETH');
    expect(dataValues(cell(eth, 'Quantity'))).toEqual(['2.718281828459045235']);
    // Shown to 8 places, rounded; the exact value is in the attribute.
    expect(cell(eth, 'Quantity').textContent.trim()).toBe('2.71828183');
    expect(dataValues(cell(eth, 'Invested (USD)'))).toEqual(['8154.845485377135705000']);
    expect(cell(eth, 'Invested (USD)').textContent.trim()).toBe('8,154.85');
  });

  it('shows an unpriced asset with a reason for its missing value, and no zero', async () => {
    openDashboard();

    const eth = await positionRow('ETH');
    expect(cell(eth, 'Average cost (USD)').textContent.trim()).toBe('3,000.00');
    expectDash(cell(eth, 'Price (USD)'));
    expect(cell(eth, 'Market value (USD)').textContent.trim()).toBe(
      MARKET_VALUE_UNAVAILABLE_MESSAGES.unsupported_pair,
    );
    expect(cell(eth, 'Market value (USD)').querySelector('data')).toBeNull();
    expectDash(cell(eth, 'Unrealized P&L (USD)'));
    expectDash(cell(eth, 'Return'));
  });

  it('says how much of a holding has no known cost, beside the quantity', async () => {
    openDashboard();

    const kas = await positionRow('KAS');
    expect(dataValues(cell(kas, 'Quantity'))).toEqual([
      '1500.000000000000000000',
      '500.000000000000000000',
    ]);
    expect(cell(kas, 'Quantity')).toHaveTextContent(/^1,500 \(500 with no known cost\)$/);
    // Average cost, invested and P&L cover the known-cost part; the value covers all 1500.
    expect(cell(kas, 'Average cost (USD)').textContent.trim()).toBe('0.10');
    expect(cell(kas, 'Invested (USD)').textContent.trim()).toBe('100.00');
    expect(cell(kas, 'Market value (USD)').textContent.trim()).toBe('120.00');
    expect(cell(kas, 'Unrealized P&L (USD)').textContent.trim()).toBe('-20.00');
    expect(cell(kas, 'Return').textContent.trim()).toBe('-20.00%');

    // A holding with every unit of known cost says nothing of the kind.
    expect(cell(await positionRow('BTC'), 'Quantity')).not.toHaveTextContent(/no known cost/);
  });

  it('shows a unit price finer than a cent to eight places, and amounts to the cent', async () => {
    // KAS 1000 bought for 84.912345678, priced at 0.084912345678: at two places the price
    // would read 0.08, and 1000 x 0.08 is not the 84.91 beside it.
    const kas = position({
      asset: 'KAS',
      quantity: '1000.000000000000000000',
      average_cost: '0.084912345678000000',
      total_invested: '84.912345678000000000',
      realized_pnl: ZERO,
      price: accountingPrice({ amount: '0.084912345678', source: 'kaspa' }),
      market_value: '84.912345678000000000',
      unrealized_pnl: ZERO,
      unrealized_return_pct: '0.0000',
    });
    openDashboard({
      positions: positionsResponse({
        positions: [kas],
        totals: totals({
          total_invested: '84.912345678000000000',
          market_value: '84.912345678000000000',
          unrealized_return_pct: '0.0000',
        }),
      }),
    });

    const row = await positionRow('KAS');
    expect(cell(row, 'Price (USD)').textContent.trim()).toBe('0.08491235');
    expect(dataValues(cell(row, 'Price (USD)'))).toEqual(['0.084912345678']);
    expect(cell(row, 'Average cost (USD)').textContent.trim()).toBe('0.08491235');
    expect(cell(row, 'Invested (USD)').textContent.trim()).toBe('84.91');
    expect(cell(row, 'Market value (USD)').textContent.trim()).toBe('84.91');
  });

  it('shows "—" for invested and unrealized P&L when no unit held has a known cost (N1)', async () => {
    // KAS: 500 held, all 500 of unknown cost. Its invested is 0 and its unrealized P&L 0 on
    // the wire, which beside a value of 40 would read as a break-even. There is no cost at all.
    const kas = position({
      asset: 'KAS',
      quantity: '500.000000000000000000',
      unknown_basis_quantity: '500.000000000000000000',
      average_cost: null,
      total_invested: ZERO,
      realized_pnl: ZERO,
      flags: ['unknown_basis'],
      price: accountingPrice({ amount: '0.080000000000', source: 'kaspa' }),
      market_value: '40.000000000000000000',
      unrealized_pnl: ZERO,
      unrealized_return_pct: null,
    });
    openDashboard({
      positions: positionsResponse({
        positions: [position(), kas],
        totals: totals(BTC_ONLY_TOTALS),
      }),
    });

    const row = await positionRow('KAS');
    expectDash(cell(row, 'Invested (USD)'));
    expectDash(cell(row, 'Unrealized P&L (USD)'));
    expectDash(cell(row, 'Average cost (USD)'));
    expectDash(cell(row, 'Return'));
    // What it is worth is known, over every unit.
    expect(cell(row, 'Market value (USD)').textContent.trim()).toBe('40.00');
    expect(cell(row, 'Quantity')).toHaveTextContent(/^500 \(500 with no known cost\)$/);
    expect(row.querySelector('th')).toHaveAccessibleName('KAS Unknown cost Not in totals');
    // A holding with some known cost keeps its figures.
    expect(cell(await positionRow('BTC'), 'Invested (USD)').textContent.trim()).toBe('52,500.00');
  });

  it('says a value too large to show is missing, and leaves it out as having no market value', async () => {
    // 2e15 BTC at 60000 is 1.2e20: past what a figure can hold (spec 021, R6). It has a price
    // and no market value, which the unpriced exclusion's reason still describes (N3).
    const btc = position({
      quantity: '2000000000000000.000000000000000000',
      average_cost: '1.000000000000000000',
      total_invested: '2000000000000000.000000000000000000',
      realized_pnl: ZERO,
      market_value: null,
      market_value_unavailable_reason: 'value_out_of_range',
      unrealized_pnl: null,
      unrealized_return_pct: null,
    });
    openDashboard({ positions: positionsResponse({ positions: [btc] }) });

    const row = await positionRow('BTC');
    expect(cell(row, 'Market value (USD)').textContent.trim()).toBe(
      MARKET_VALUE_UNAVAILABLE_MESSAGES.value_out_of_range,
    );
    expect(dataValues(cell(row, 'Price (USD)'))).toEqual(['60000.000000000000']);
    expect(row.querySelector('th')).toHaveAccessibleName('BTC Not in totals');
    const region = await loadedRegion();
    expect(
      within(region).getByText('Left out of these totals:').nextElementSibling?.textContent,
    ).toBe(`BTC: ${EXCLUSION_REASON_MESSAGES.unpriced}`);
    expectDash(await summaryValue('Invested'));
  });

  it('puts only wire strings in every <data value> of the section', async () => {
    const response = investedPortfolio();
    openDashboard({ positions: response });

    const region = await loadedRegion();
    await positionsTable();
    const sent = wireStrings(response);
    const values = dataValues(region);

    expect(values.length).toBeGreaterThan(20);
    for (const value of values) {
      expect(sent).toContain(value);
    }
  });

  it('marks the numeric columns, and makes the table a keyboard-reachable region', async () => {
    openDashboard();

    const region = await loadedRegion();
    expect(within(region).getByRole('heading', { level: 2, name: 'Invested' })).toBeInTheDocument();
    expect(
      within(region).getByRole('heading', { level: 3, name: 'Per asset' }),
    ).toBeInTheDocument();

    const scroller = within(region).getByRole('region', { name: 'Per asset' });
    expect(scroller).toHaveAttribute('tabindex', '0');
    expect(scroller).toContainElement(await positionsTable());
  });
});

describe('InvestedSection: the summary (criterion 2)', () => {
  it('shows invested against market value, unrealized P&L with its return, and realized P&L', async () => {
    openDashboard();

    const invested = await summaryValue('Invested');
    expect(dataValues(invested)).toEqual([INVESTED_TOTALS.invested]);
    expect(invested).toHaveTextContent(/^52,500\.00 USD$/);

    const value = await summaryValue('Market value');
    expect(dataValues(value)).toEqual([INVESTED_TOTALS.marketValue]);
    expect(value).toHaveTextContent(/^90,000\.00 USD$/);

    const unrealized = await summaryValue('Unrealized P&L');
    expect(dataValues(unrealized)).toEqual([
      INVESTED_TOTALS.unrealizedPnl,
      INVESTED_TOTALS.returnPct,
    ]);
    expect(unrealized).toHaveTextContent('+37,500.00 USD');
    expect(unrealized).toHaveTextContent('Return +71.43%');

    const realized = await summaryValue('Realized P&L');
    expect(dataValues(realized)).toEqual([INVESTED_TOTALS.realizedPnl]);
    expect(realized).toHaveTextContent(/^\+7,375\.50 USD$/);
  });

  it('names what is left out of the totals and why, grouping assets that share a reason', async () => {
    openDashboard();

    const region = await loadedRegion();
    const heading = within(region).getByText('Left out of these totals:');
    const list = heading.nextElementSibling;
    if (!(list instanceof HTMLElement)) {
      throw new Error('No list follows "Left out of these totals:".');
    }
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual([
      `ETH: ${EXCLUSION_REASON_MESSAGES.unpriced}`,
      `KAS, SOL: ${EXCLUSION_REASON_MESSAGES.unknown_basis}`,
    ]);
    // SOL is both unknown-basis and unpriced: named once, as unknown-basis (R4).
    expect(list.textContent.match(/SOL/g)).toHaveLength(1);
    // Realized P&L is summed over every position, these included, and the page says so.
    expect(within(region).getByText(/Realized P&L covers every position/)).toBeInTheDocument();
  });

  it('shows "—" rather than an empty sum when every held position is excluded', async () => {
    // ETH is unpriced and KAS has units of no known cost; XRP is counted, holding nothing.
    // The backend's invested, value and unrealized totals are then zeros over nothing held.
    openDashboard({ positions: everyHeldPositionExcluded() });

    for (const term of ['Invested', 'Market value', 'Unrealized P&L']) {
      expectDash(await summaryValue(term));
    }
    // Realized P&L covers every position, so it is a real figure: -250 + 0 + 125.5.
    const realized = await summaryValue('Realized P&L');
    expect(dataValues(realized)).toEqual(['-124.500000000000000000']);
    expect(realized).toHaveTextContent(/^-124\.50 USD$/);

    const summary = realized.closest('dl');
    if (summary === null) {
      throw new Error('The summary is not a <dl>.');
    }
    expectNoRenderedZero(summary);
  });

  it('shows a genuine zero when nothing is held at all', async () => {
    // Every asset has been sold. Nothing is held, so nothing is left out: the invested total
    // over the closed positions is a true 0.00, like the value section's genuinely empty
    // portfolio (spec 011). Spec 022's "—" is for held positions that are all excluded.
    openDashboard({
      positions: positionsResponse({
        positions: [xrpClosed()],
        totals: totals({ realized_pnl: '125.500000000000000000' }),
      }),
    });

    expect(dataValues(await summaryValue('Invested'))).toEqual([ZERO]);
    expect(await summaryValue('Invested')).toHaveTextContent(/^0\.00 USD$/);
    expect(await summaryValue('Unrealized P&L')).toHaveTextContent('Return —');
    expect(await summaryValue('Realized P&L')).toHaveTextContent(/^\+125\.50 USD$/);
  });

  it('says what costs belong to no asset, naming both of their origins (S3)', async () => {
    openDashboard();

    const region = await loadedRegion();
    const line = within(region).getByText(/Costs not assigned to any asset/);
    expect(line.textContent).toBe(UNALLOCATED_LINE);
    expect(dataValues(line)).toEqual([INVESTED_TOTALS.unallocatedCosts]);
  });

  it('says the totals include a stale price when a position in them has one (S2)', async () => {
    const stale = accountingPrice({ as_of: STALE_PRICE_AS_OF, stale: true });
    openDashboard({
      positions: positionsResponse({
        positions: [position({ price: stale })],
        totals: totals(BTC_ONLY_TOTALS),
      }),
    });

    const region = await loadedRegion();
    expect(within(region).getByText(STALE_TOTALS)).toBeInTheDocument();
    expect(cell(await positionRow('BTC'), 'Price (USD)')).toHaveTextContent(
      /\(stale, as of 2 hours ago\)$/,
    );
  });

  it('says nothing of a stale price that is not in the totals', async () => {
    // In the full portfolio, KAS's price is stale, and KAS is left out as unknown-basis.
    openDashboard();
    await positionRow('KAS');
    expect(within(await loadedRegion()).queryByText(STALE_TOTALS)).not.toBeInTheDocument();
  });

  it('says nothing of a stale price on a position no longer held', async () => {
    const kasClosed = xrpClosed({
      asset: 'KAS',
      realized_pnl: ZERO,
      price: accountingPrice({
        amount: '0.080000000000',
        source: 'kaspa',
        as_of: STALE_PRICE_AS_OF,
        stale: true,
      }),
    });
    openDashboard({
      positions: positionsResponse({
        positions: [position(), kasClosed],
        totals: totals(BTC_ONLY_TOTALS),
      }),
    });

    await positionRow('BTC');
    expect(within(await loadedRegion()).queryByText(STALE_TOTALS)).not.toBeInTheDocument();
  });

  it('warns that realized P&L may be inaccurate, naming every asset whose history is short (M1)', async () => {
    // BGB is closed and history_incomplete; ETH is held and history_incomplete; SOL was charged
    // a fee that could not be valued. Realized P&L covers all three, held or not.
    openDashboard();

    const region = await loadedRegion();
    expect(within(region).getByText(realizedCaveat('BGB, ETH, and SOL'))).toBeInTheDocument();
  });

  it('names the one asset when only one is affected, even with no row of its own', async () => {
    openDashboard({
      positions: positionsResponse({
        positions: [bgbFeeNeverHeld(), position()],
        totals: totals(BTC_ONLY_TOTALS),
        warnings: feeInNeverHeldAsset({ charged_to: null }),
      }),
    });

    await positionRow('BTC');
    expect(within(await loadedRegion()).getByText(realizedCaveat('BGB'))).toBeInTheDocument();
  });

  it('says nothing about fees, exclusions or a legend when there are none', async () => {
    openDashboard({ positions: breakEvenPortfolio() });

    const region = await loadedRegion();
    await positionsTable();
    expect(within(region).queryByText(/Costs not assigned/)).not.toBeInTheDocument();
    expect(within(region).queryByText(/Realized P&L may be inaccurate/)).not.toBeInTheDocument();
    expect(within(region).queryByText(STALE_TOTALS)).not.toBeInTheDocument();
    expect(within(region).queryByText(/Left out of these totals/)).not.toBeInTheDocument();
    expect(
      within(region).queryByText(/Realized P&L covers every position/),
    ).not.toBeInTheDocument();
    expect(within(region).queryByText('Not in totals')).not.toBeInTheDocument();
    expect(legend(region)).toEqual([]);
  });
});

describe('InvestedSection: the sign of P&L (criterion 4)', () => {
  it('signs a loss with a minus, in the table and in the summary', async () => {
    // KAS: value 80 against a cost of 120, -40; -40 / 120 = -33.3333%. A sale realized -15.
    openDashboard({ positions: kasLossPortfolio() });

    const kas = await positionRow('KAS');
    expect(cell(kas, 'Unrealized P&L (USD)').textContent.trim()).toBe('-40.00');
    expect(dataValues(cell(kas, 'Unrealized P&L (USD)'))).toEqual(['-40.000000000000000000']);
    expect(cell(kas, 'Return').textContent.trim()).toBe('-33.33%');

    const unrealized = await summaryValue('Unrealized P&L');
    expect(unrealized).toHaveTextContent('-40.00 USD');
    expect(unrealized).toHaveTextContent('Return -33.33%');
    expect(unrealized.textContent).not.toMatch(/\+/);
    expect(await summaryValue('Realized P&L')).toHaveTextContent(/^-15\.00 USD$/);
  });

  it('leaves a break-even position unsigned', async () => {
    openDashboard({ positions: breakEvenPortfolio() });

    const btc = await positionRow('BTC');
    expect(cell(btc, 'Unrealized P&L (USD)').textContent.trim()).toBe('0.00');
    expect(cell(btc, 'Return').textContent.trim()).toBe('0.00%');

    const unrealized = await summaryValue('Unrealized P&L');
    expect(unrealized).toHaveTextContent('0.00 USD');
    expect(unrealized).toHaveTextContent('Return 0.00%');
    expect(unrealized.textContent).not.toMatch(/[+-]/);
    expect((await summaryValue('Realized P&L')).textContent).not.toMatch(/[+-]/);
  });

  it('keeps the sign of a gain or a loss too small to show', async () => {
    // Unrealized +0.000000004 and realized -0.003: neither is a zero, so neither shows one.
    openDashboard({ positions: tinyPnlPortfolio() });

    const btc = await positionRow('BTC');
    expect(cell(btc, 'Unrealized P&L (USD)').textContent.trim()).toBe('< +0.01');
    expect(dataValues(cell(btc, 'Unrealized P&L (USD)'))).toEqual(['0.000000004000000000']);
    expect(await summaryValue('Unrealized P&L')).toHaveTextContent('< +0.01 USD');
    expect(await summaryValue('Realized P&L')).toHaveTextContent(/^> -0\.01 USD$/);
    expect(dataValues(await summaryValue('Realized P&L'))).toEqual(['-0.003000000000000000']);
  });
});

describe('InvestedSection: flags and exclusions (criteria 5 and 8)', () => {
  it('marks an unknown-basis asset, leaves it out of the totals, says why and explains the flag', async () => {
    openDashboard({
      positions: positionsResponse({
        positions: [position(), kasUnknownBasis()],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7500.000000000000000000',
        }),
      }),
    });

    const kas = await positionRow('KAS');
    expect(kas.querySelector('th')).toHaveAccessibleName('KAS Unknown cost Not in totals');
    expect((await positionRow('BTC')).querySelector('th')).toHaveAccessibleName('BTC');

    const region = await loadedRegion();
    const leftOut = within(region).getByText('Left out of these totals:').nextElementSibling;
    expect(leftOut?.textContent).toBe(`KAS: ${EXCLUSION_REASON_MESSAGES.unknown_basis}`);

    expect(legend(region)).toEqual([
      { badge: FLAG_BADGES.unknown_basis, explanation: FLAG_EXPLANATIONS.unknown_basis },
    ]);
    // Sales of unknown-cost units are kept out of realized P&L, so it is not called unreliable.
    expect(within(region).queryByText(/Realized P&L may be inaccurate/)).not.toBeInTheDocument();
  });

  it('leaves an unpriced asset out of the totals with its own reason', async () => {
    openDashboard({
      positions: positionsResponse({
        positions: [position(), ethUnpriced({ flags: [] })],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7250.000000000000000000',
        }),
      }),
    });

    const eth = await positionRow('ETH');
    // No flag: "Not in totals" alone.
    expect(eth.querySelector('th')).toHaveAccessibleName('ETH Not in totals');

    const region = await loadedRegion();
    const leftOut = within(region).getByText('Left out of these totals:').nextElementSibling;
    expect(leftOut?.textContent).toBe(`ETH: ${EXCLUSION_REASON_MESSAGES.unpriced}`);
    expect(legend(region)).toEqual([]);
  });

  it('marks a history_incomplete asset and explains it, without leaving it out of the totals', async () => {
    // A BTC sale larger than the history held: its shortfall is the warning that set the flag.
    openDashboard({
      positions: positionsResponse({
        positions: [position({ flags: ['history_incomplete'] })],
        totals: totals(BTC_ONLY_TOTALS),
        warnings: [warning({ asset: 'BTC', quantity: '0.100000000000000000' })],
      }),
    });

    const btc = await positionRow('BTC');
    expect(btc.querySelector('th')).toHaveAccessibleName('BTC History incomplete');
    const region = await loadedRegion();
    expect(legend(region)).toEqual([
      { badge: 'History incomplete', explanation: FLAG_EXPLANATIONS.history_incomplete },
    ]);
    // It does not exclude: the totals include BTC, and nothing is listed as left out.
    expect(within(region).queryByText(/Left out of these totals/)).not.toBeInTheDocument();
    expect(dataValues(await summaryValue('Invested'))).toEqual(['52500.000000000000000000']);
    expect(within(region).getByText(realizedCaveat('BTC'))).toBeInTheDocument();
  });

  it('marks an unattributed_fee asset and explains it, without leaving it out of the totals', async () => {
    // A BTC buy paid its fee in BGB, which the history never held: the engine opens a BGB pool,
    // finds it short, and cannot value the fee (S4). So BGB is closed and history_incomplete.
    openDashboard({
      positions: positionsResponse({
        positions: [bgbFeeNeverHeld(), position({ flags: ['unattributed_fee'] })],
        totals: totals(BTC_ONLY_TOTALS),
        warnings: feeInNeverHeldAsset({ charged_to: 'BTC' }),
      }),
    });

    const btc = await positionRow('BTC');
    expect(btc.querySelector('th')).toHaveAccessibleName('BTC Fee not valued');
    const region = await loadedRegion();
    expect(legend(region)).toEqual([
      { badge: 'History incomplete', explanation: FLAG_EXPLANATIONS.history_incomplete },
      { badge: 'Fee not valued', explanation: FLAG_EXPLANATIONS.unattributed_fee },
    ]);
    expect(within(region).queryByText(/Left out of these totals/)).not.toBeInTheDocument();
    expect(dataValues(await summaryValue('Invested'))).toEqual(['52500.000000000000000000']);
    expect(within(region).getByText(realizedCaveat('BGB and BTC'))).toBeInTheDocument();
  });

  it('marks a position that is both unknown-basis and unpriced, and leaves it out once', async () => {
    openDashboard();

    const sol = await positionRow('SOL');
    expect(sol.querySelector('th')).toHaveAccessibleName(
      'SOL Fee not valued Unknown cost Not in totals',
    );
    expect(within(sol).getAllByText('Not in totals')).toHaveLength(1);
    expect(dataValues(cell(sol, 'Quantity'))).toEqual([
      '10.000000000000000000',
      '4.000000000000000000',
    ]);
    expect(cell(sol, 'Market value (USD)').textContent.trim()).toBe(
      MARKET_VALUE_UNAVAILABLE_MESSAGES.unsupported_pair,
    );
  });

  it('explains each flag shown anywhere once, alphabetically', async () => {
    // history_incomplete is on ETH's row and on BGB, which has no row; it is explained once.
    openDashboard();

    expect(legend(await loadedRegion())).toEqual([
      { badge: 'History incomplete', explanation: FLAG_EXPLANATIONS.history_incomplete },
      { badge: 'Fee not valued', explanation: FLAG_EXPLANATIONS.unattributed_fee },
      { badge: 'Unknown cost', explanation: FLAG_EXPLANATIONS.unknown_basis },
    ]);
  });

  it('keeps a flag visible when only an asset no longer held carries it (M1)', async () => {
    // A stablecoin conversion paid its fee in BGB, never held: BGB is closed and
    // history_incomplete, and no row carries the flag. It is named in the closed line,
    // explained in the legend, and it qualifies realized P&L.
    openDashboard({
      positions: positionsResponse({
        positions: [bgbFeeNeverHeld(), position()],
        totals: totals(BTC_ONLY_TOTALS),
        warnings: feeInNeverHeldAsset({ charged_to: null }),
      }),
    });

    expect((await positionRow('BTC')).querySelector('th')).toHaveAccessibleName('BTC');
    const region = await loadedRegion();
    expect(
      within(region).getByText(
        '1 asset no longer held is not listed: BGB (History incomplete). ' +
          'Its realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
    expect(legend(region)).toEqual([
      { badge: 'History incomplete', explanation: FLAG_EXPLANATIONS.history_incomplete },
    ]);
    expect(within(region).getByText(realizedCaveat('BGB'))).toBeInTheDocument();
  });
});

describe('InvestedSection: a stale price (criterion 6)', () => {
  it('says the price is stale and when it is from, in a <time> carrying its as_of', async () => {
    openDashboard();

    const price = cell(await positionRow('KAS'), 'Price (USD)');
    expect(dataValues(price)).toEqual(['0.080000000000']);
    expect(price).toHaveTextContent(/^0\.08 \(stale, as of 2 hours ago\)$/);
    const time = price.querySelector('time');
    expect(time?.getAttribute('datetime')).toBe(STALE_PRICE_AS_OF);
    // Not a live region: the phrase ticks.
    expect(price.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
  });
});

describe('InvestedSection: closed positions', () => {
  it('lists an asset no longer held in one line, not as a row', async () => {
    // Realized: 7500 (BTC) + 125.5 (XRP) = 7625.5.
    openDashboard({
      positions: positionsResponse({
        positions: [position(), xrpClosed()],
        totals: totals({ ...BTC_ONLY_TOTALS, realized_pnl: '7625.500000000000000000' }),
      }),
    });

    const region = await loadedRegion();
    expect(await rowSymbols()).toEqual(['BTC']);
    expect(
      within(region).getByText(
        '1 asset no longer held is not listed: XRP. Its realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
  });

  it('names the flags of the assets no longer held, beside the flagged ones', async () => {
    openDashboard();

    expect(await rowSymbols()).not.toContain('BGB');
    expect(
      within(await loadedRegion()).getByText(
        '2 assets no longer held are not listed: BGB (History incomplete), XRP. ' +
          'Their realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
  });

  it('lists several assets no longer held in the plural', async () => {
    // Realized: 7500 (BTC) - 3.25 (DOGE) + 125.5 (XRP) = 7622.25.
    openDashboard({
      positions: positionsResponse({
        positions: [
          position(),
          xrpClosed({ asset: 'DOGE', realized_pnl: '-3.250000000000000000' }),
          xrpClosed(),
        ],
        totals: totals({
          total_invested: '52500.000000000000000000',
          market_value: '90000.000000000000000000',
          unrealized_pnl: '37500.000000000000000000',
          unrealized_return_pct: '71.4286',
          realized_pnl: '7622.250000000000000000',
        }),
      }),
    });

    expect(await rowSymbols()).toEqual(['BTC']);
    expect(
      within(await loadedRegion()).getByText(
        '2 assets no longer held are not listed: DOGE, XRP. Their realized P&L is in the total.',
      ),
    ).toBeInTheDocument();
  });

  it('says nothing is held, instead of a table, when every asset is sold', async () => {
    openDashboard({
      positions: positionsResponse({
        positions: [xrpClosed()],
        totals: totals({ realized_pnl: '125.500000000000000000' }),
      }),
    });

    const region = await loadedRegion();
    expect(await within(region).findByText('Nothing is held right now.')).toBeInTheDocument();
    expect(within(region).queryByRole('table')).not.toBeInTheDocument();
    expect(
      within(region).getByText(/^1 asset no longer held is not listed: XRP\./),
    ).toBeInTheDocument();
  });

  it('says nothing about sold assets when there are none', async () => {
    openDashboard({ positions: breakEvenPortfolio() });

    await positionsTable();
    expect(within(await loadedRegion()).queryByText(/no longer held/)).not.toBeInTheDocument();
  });
});

describe('InvestedSection: how the figures were computed', () => {
  it('says the method, the currency and when, outside any live region, and that it is not a tax figure', async () => {
    openDashboard();

    const line = within(await loadedRegion()).getByText(/Weighted average cost in/);
    expect(line.textContent).toBe(
      'Weighted average cost in USD, computed 15 minutes ago. Not a tax figure.',
    );
    expect(line.querySelector('time')?.getAttribute('datetime')).toBe(COMPUTED_AT);
    expect(line.closest('[role="status"], [role="alert"], [aria-live]')).toBeNull();
  });

  it('keeps each section in its own currency', async () => {
    // The balances are in EUR, the invested figures in USD: two currencies, each labelled.
    openDashboard();

    const total = await screen.findByRole('region', { name: 'Total value' });
    expect(total).toHaveTextContent('EUR');
    expect(total).not.toHaveTextContent('USD');
    expect(await summaryValue('Invested')).toHaveTextContent('USD');
    expect(await summaryValue('Invested')).not.toHaveTextContent('EUR');
  });

  it('warns that a failed recompute left these figures behind, with an instant that does not tick', async () => {
    openDashboard({ positions: investedPortfolio({ last_recompute: failedRecompute() }) });

    const region = await loadedRegion();
    // Two alerts say it, each about its own figures: the section's, and below it the holdings
    // check's, which compares nothing against a history that is behind (spec 025, R9).
    await waitFor(() => {
      expect(within(region).getAllByRole('alert')).toHaveLength(2);
    });
    const [alert, holdingsAlert] = within(region).getAllByRole('alert');
    if (alert === undefined) {
      throw new Error('The section raised no alert.');
    }
    expect(holdingsAlert).toHaveTextContent(
      /may be older than the balances and nothing is compared/,
    );
    expect(alert).toHaveTextContent(
      /^The last recompute failed on .+ \(UnconvertibleFillError\)\./,
    );
    expect(alert).toHaveTextContent('These figures were computed before that.');
    expect(alert.querySelector('time')?.getAttribute('datetime')).toBe(RECOMPUTE_FAILED_AT);
    // A relative phrase re-announces itself inside a live region (spec 016).
    expect(alert).not.toHaveTextContent(/ago|just now/);
    // The figures stay.
    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
  });

  it('warns that a venue whose sync failed may be missing trades, and links to it', async () => {
    openDashboard({
      exchanges: [authFailedExchange('auth', { exchange_key: 'bingx' }), exchange()],
    });

    const region = await loadedRegion();
    const alert = await within(region).findByRole('alert');
    expect(alert).toHaveTextContent(
      'These figures may miss recent trades from BingX: the last sync failed.',
    );
    expect(within(alert).getByRole('link', { name: 'Open exchanges' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    await positionRow('BTC');
  });

  it('raises no alert when nothing is wrong', async () => {
    openDashboard();

    const region = await loadedRegion();
    await positionsTable();
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(region).queryByRole('status')).not.toBeInTheDocument();
  });

  it('warns when exchange status is unavailable, and keeps the figures', async () => {
    openDashboard({
      before: ({ exchanges }) => {
        exchanges.fail('list', () =>
          problem(503, 'Service Unavailable', 'The exchange list is being rebuilt.'),
        );
      },
    });

    const alert = await within(await loadedRegion()).findByRole('alert');
    expect(alert).toHaveTextContent(
      'Exchange status is unavailable: The exchange list is being rebuilt. ' +
        'A failed exchange sync cannot be ruled out.',
    );
    await positionRow('BTC');
  });
});

describe('InvestedSection: history warnings', () => {
  it('lists what the history could not account for, collapsed, with when, where and how much', async () => {
    openDashboard();

    const region = await loadedRegion();
    const details = region.querySelector('details');
    if (details === null) {
      throw new Error('No history warnings.');
    }
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')?.textContent).toBe(
      'What the imported history could not account for (3)',
    );

    const [shortSale, feeShortfall, fee] = within(details).getAllByRole('listitem');
    expect(shortSale?.querySelector('time')?.getAttribute('datetime')).toBe(WARNING_OCCURRED_AT);
    expect(shortSale).toHaveTextContent(
      /on Bitget: A sale of, or a fee paid in, ETH exceeded the imported history by 0\.25 ETH\. A buy or a deposit is missing\.$/,
    );
    expect(dataValues(shortSale ?? details)).toEqual(['0.250000000000000000']);
    // The fee paid in BGB, never held, is a shortfall and an unvalued fee at one moment.
    expect(feeShortfall?.querySelector('time')?.getAttribute('datetime')).toBe(FEE_OCCURRED_AT);
    expect(feeShortfall).toHaveTextContent(
      /on BingX: A sale of, or a fee paid in, BGB exceeded the imported history by 0\.002 BGB\. A buy or a deposit is missing\.$/,
    );
    expect(fee?.querySelector('time')?.getAttribute('datetime')).toBe(FEE_OCCURRED_AT);
    expect(fee).toHaveTextContent(
      /on BingX: A fee of 0\.002 BGB could not be valued\. It is left out of the figures for SOL\.$/,
    );
  });

  it('names a venue it does not know by its source, and a stablecoin conversion fee as such', async () => {
    openDashboard({
      positions: investedPortfolio({
        warnings: [
          warning(),
          ...feeInNeverHeldAsset({ charged_to: 'SOL' }),
          // A stablecoin conversion on a venue this build does not know, fee paid in BGB.
          ...feeInNeverHeldAsset({
            source: 'kraken',
            occurred_at: '2026-06-10T08:00:00Z',
            quantity: '0.100000000000000000',
            charged_to: null,
          }),
        ],
      }),
    });

    const details = (await loadedRegion()).querySelector('details');
    expect(details?.querySelector('summary')?.textContent).toBe(
      'What the imported history could not account for (5)',
    );
    const items = within(details ?? document.body).getAllByRole('listitem');
    expect(items.at(-1)).toHaveTextContent(
      /on kraken: A fee of 0\.1 BGB could not be valued\. It was paid on a conversion between stablecoins\.$/,
    );
  });

  it('shows no warnings section when there are none', async () => {
    openDashboard({ positions: breakEvenPortfolio() });

    await positionsTable();
    expect((await loadedRegion()).querySelector('details')).toBeNull();
  });
});

describe('InvestedSection: empty states (criterion 7)', () => {
  /** The section's heading at level 3, below "Invested". */
  async function emptyHeading(name: string): Promise<HTMLElement> {
    return within(await loadedRegion()).findByRole('heading', { level: 3, name });
  }

  async function expectNoFigures(): Promise<void> {
    const region = await loadedRegion();
    expect(region.querySelector('data')).toBeNull();
    expect(within(region).queryByRole('table')).not.toBeInTheDocument();
    expect(region.textContent).not.toMatch(ZERO_AMOUNT);
  }

  it('row 1: the recompute failed - when, the error class, and where to look', async () => {
    openDashboard({ positions: failedFirstRecompute(), exchanges: [exchange()] });

    const heading = await emptyHeading('Positions could not be computed');
    const alert = heading.closest('[role="alert"]');
    if (!(alert instanceof HTMLElement)) {
      throw new Error('A failed recompute is not announced.');
    }
    expect(alert).toHaveTextContent(`(${RECOMPUTE_ERROR})`);
    expect(alert).toHaveTextContent(/tried again after the next exchange sync/);
    expect(alert.querySelector('time')?.getAttribute('datetime')).toBe(RECOMPUTE_FAILED_AT);
    expect(alert).not.toHaveTextContent(/ago|just now/);
    expect(within(alert).getByRole('link', { name: 'Open exchanges' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    await expectNoFigures();
  });

  it('row 2: the exchange sync failed - which venue, and where to fix it', async () => {
    openDashboard({ positions: emptySnapshot(), exchanges: [authFailedExchange('auth')] });

    const heading = await emptyHeading('The exchange sync failed');
    const alert = heading.closest('[role="alert"]');
    if (!(alert instanceof HTMLElement)) {
      throw new Error('A failed sync is not announced.');
    }
    expect(alert).toHaveTextContent(
      'Trades from Bitget may be missing: the last sync failed. ' +
        'Positions are computed from imported trades.',
    );
    expect(within(alert).getByRole('link', { name: 'Open exchanges' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    // The refused venue stored nothing, and "no trades imported yet" is not what is shown.
    expect(
      screen.queryByRole('heading', { name: 'No trades imported yet' }),
    ).not.toBeInTheDocument();
    await expectNoFigures();
  });

  it('row 2: names every venue whose sync failed', async () => {
    openDashboard({
      positions: emptySnapshot(),
      exchanges: [
        authFailedExchange('insufficient_scope', { exchange_key: 'bingx' }),
        erroredExchange('unavailable'),
      ],
    });

    const alert = (await emptyHeading('The exchange sync failed')).closest('[role="alert"]');
    expect(alert).toHaveTextContent('Trades from BingX and Bitget may be missing');
  });

  it('row 3: no trades imported yet, with no venue configured', async () => {
    openDashboard({ positions: emptySnapshot(), exchanges: [] });

    const heading = await emptyHeading('No trades imported yet');
    const region = await loadedRegion();
    expect(heading.closest('[role="alert"]')).toBeNull();
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
    expect(region).toHaveTextContent(
      'An exchange must be configured on the server before trades can be imported.',
    );
    expect(within(region).getByRole('link', { name: 'Open exchanges' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    await expectNoFigures();
  });

  it('row 3: no trades imported yet, with a venue configured and never synced', async () => {
    openDashboard({ positions: emptySnapshot(), exchanges: [unsyncedExchange('bingx')] });

    await emptyHeading('No trades imported yet');
    const region = await loadedRegion();
    expect(region).toHaveTextContent('Syncing the exchanges imports your trades.');
    expect(region).not.toHaveTextContent(/must be configured/);
  });

  it('row 4: not computed yet', async () => {
    openDashboard({ positions: noSnapshot(), exchanges: [exchange()] });

    await emptyHeading('Positions have not been computed yet');
    expect(await loadedRegion()).toHaveTextContent(
      'They are computed at startup and after each exchange sync that stores a trade.',
    );
    await expectNoFigures();
  });

  it('row 4: a snapshot over no trades, while fills are stored, predates them (S1)', async () => {
    // event_count 0 proves the snapshot replayed nothing, and the venue holds 1234 fills: it
    // is not "no positions", which would claim every trade was between stablecoins.
    openDashboard({ positions: emptySnapshot(), exchanges: [exchange()] });

    await emptyHeading('Positions have not been computed yet');
    const region = await loadedRegion();
    expect(within(region).queryByRole('heading', { name: 'No positions' })).toBeNull();
    expect(within(region).queryByRole('heading', { name: 'No trades imported yet' })).toBeNull();
  });

  it('row 5: no positions, because every trade was between stablecoins', async () => {
    openDashboard({ positions: stablecoinOnlySnapshot(), exchanges: [exchange()] });

    await emptyHeading('No positions');
    expect(await loadedRegion()).toHaveTextContent(
      'Every trade imported so far is between stablecoins, which are held at cost.',
    );
    await expectNoFigures();
  });

  it('believes the snapshot over a list that lags it: trades replayed are not "no trades" (R9 N-a)', async () => {
    // The list was polled before a stablecoin-only sync landed and still says 0 fills; the
    // snapshot, written after it, replayed 4 events. "No trades imported yet" would contradict it.
    openDashboard({ positions: stablecoinOnlySnapshot(), exchanges: [unsyncedExchange('bingx')] });

    await emptyHeading('No positions');
    const region = await loadedRegion();
    expect(within(region).queryByRole('heading', { name: 'No trades imported yet' })).toBeNull();
    expect(region).not.toHaveTextContent(/Syncing the exchanges imports/);
  });

  it('falls back when the exchanges query fails: what the snapshot proves, and nothing it does not (S1)', async () => {
    // A snapshot over no events proves no trade has been replayed, list or no list. What it
    // cannot say is whether a venue is configured, so the description says neither.
    openDashboard({
      positions: emptySnapshot(),
      before: ({ exchanges }) => {
        exchanges.fail('list', () => HttpResponse.error());
      },
    });

    await emptyHeading('No trades imported yet');
    const region = await loadedRegion();
    expect(region).toHaveTextContent(NO_TRADES_NEUTRAL);
    expect(region).not.toHaveTextContent(/must be configured|Syncing the exchanges/);
    expect(within(region).getByRole('link', { name: 'Open exchanges' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    expect(within(region).getByRole('alert')).toHaveTextContent(
      'Exchange status is unavailable: The exchange list could not be read. ' +
        'A failed exchange sync cannot be ruled out.',
    );
    expect(within(region).queryByRole('heading', { name: 'No positions' })).toBeNull();
    expect(within(region).queryByRole('heading', { name: 'The exchange sync failed' })).toBeNull();
  });

  it('falls back to "no positions" only when the snapshot replayed trades (S1)', async () => {
    openDashboard({
      positions: stablecoinOnlySnapshot(),
      before: ({ exchanges }) => {
        exchanges.fail('list', () => HttpResponse.error());
      },
    });

    await emptyHeading('No positions');
    const region = await loadedRegion();
    expect(within(region).getByRole('alert')).toHaveTextContent(/Exchange status is unavailable/);
    expect(within(region).queryByRole('heading', { name: 'No trades imported yet' })).toBeNull();
  });

  it.each<[string, PositionsResponse, string]>([
    ['with positions', investedPortfolio(), 'BTC'],
    ['with none, over stablecoin trades', stablecoinOnlySnapshot(), 'No positions'],
    ['with none, over no trade', emptySnapshot(), 'No trades imported yet'],
  ])(
    'treats an exchange list kept across a failed poll as unknown, %s (R5)',
    async (_label, positions, landmark) => {
      // The list said Bitget's sync failed; then a poll of the list failed. The stale list is
      // no longer trusted either way: neither its failure nor its "no trades" is claimed.
      vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
      vi.setSystemTime(new Date(NOW));
      const { exchanges } = openDashboard({
        positions,
        exchanges: [erroredExchange('unavailable')],
      });
      const region = await loadedRegion();
      expect(await within(region).findByRole('alert')).toHaveTextContent(
        /Bitget may be missing|trades from Bitget/,
      );

      exchanges.fail('list', () => problem(503, 'Service Unavailable', 'Try again shortly.'));
      act(() => {
        vi.advanceTimersByTime(60_000);
      });

      await waitFor(() => {
        expect(within(region).getByRole('alert')).toHaveTextContent(
          'Exchange status is unavailable: Try again shortly.',
        );
      });
      // Neither sentence that names the venue's failed sync is left on screen.
      expect(region).not.toHaveTextContent(/may miss recent trades|may be missing/);
      expect(
        within(region).queryByRole('heading', { name: 'The exchange sync failed' }),
      ).toBeNull();
      expect(within(region).getAllByText(landmark).length).toBeGreaterThan(0);
    },
  );

  it('falls back to "not computed yet" when there is no snapshot and no exchange status', async () => {
    openDashboard({
      positions: noSnapshot(),
      before: ({ exchanges }) => {
        exchanges.fail('list', () => problem(503, 'Service Unavailable', 'Try again shortly.'));
      },
    });

    await emptyHeading('Positions have not been computed yet');
    expect(within(await loadedRegion()).getByRole('alert')).toHaveTextContent(
      'Exchange status is unavailable: Try again shortly.',
    );
  });

  it('waits for the exchange list before choosing an empty state', async () => {
    let release: () => void = () => undefined;
    openDashboard({
      positions: emptySnapshot(),
      exchanges: [authFailedExchange('auth')],
      before: ({ exchanges }) => {
        release = exchanges.hold('list');
      },
    });

    const region = await investedRegion();
    expect(await within(region).findByRole('status')).toHaveTextContent(
      'Loading invested per asset…',
    );
    // Neither "no trades" nor "sync failed" can be told yet, so neither is claimed: the one
    // heading under "Invested" is the holdings check's own, which reads its own query and
    // does not wait for the exchange list (spec 025).
    await within(region).findByRole('heading', { level: 3, name: 'Holdings check' });
    expect(
      within(region)
        .getAllByRole('heading', { level: 3 })
        .map((heading) => heading.textContent),
    ).toEqual(['Holdings check']);

    release();

    await emptyHeading('The exchange sync failed');
    expect(within(region).queryByRole('status')).not.toBeInTheDocument();
  });

  it('keeps one status element from the positions read to the exchanges read (N5)', async () => {
    // Both loading cases are one Skeleton from one place, so the live region is not replaced -
    // and re-announced - when the positions answer while the exchanges have not.
    let releasePositions: () => void = () => undefined;
    let releaseExchanges: () => void = () => undefined;
    const { accounting } = openDashboard({
      positions: emptySnapshot(),
      exchanges: [],
      before: (fakes) => {
        releasePositions = fakes.accounting.hold();
        releaseExchanges = fakes.exchanges.hold('list');
      },
    });

    const region = await investedRegion();
    const first = await within(region).findByRole('status');
    await waitFor(() => {
      expect(accounting.count()).toBeGreaterThan(0);
    });

    releasePositions();
    await settle();
    await settle();

    expect(within(region).getByRole('status')).toBe(first);
    expect(first).toBeInTheDocument();
    expect(first).toHaveTextContent('Loading invested per asset…');

    releaseExchanges();
    await emptyHeading('No trades imported yet');
    expect(first).not.toBeInTheDocument();
  });

  it('does not wait for the exchange list when there are positions to show', async () => {
    openDashboard({
      before: ({ exchanges }) => {
        exchanges.hold('list');
      },
    });

    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
  });
});

describe('InvestedSection: loading and failures (criterion 8)', () => {
  it('announces that invested per asset is loading, with no figure meanwhile', async () => {
    let release: () => void = () => undefined;
    openDashboard({
      before: ({ accounting }) => {
        release = accounting.hold();
      },
    });

    const region = await investedRegion();
    const status = await within(region).findByRole('status');
    expect(status).toHaveTextContent('Loading invested per asset…');
    expect(region.querySelector('data')).toBeNull();
    expect(within(region).getByRole('heading', { level: 2, name: 'Invested' })).toBeInTheDocument();
    // The value section does not wait for it.
    expect(dataValues(await screen.findByRole('region', { name: 'Total value' }))).toContain(
      HEALTHY.total,
    );

    release();

    await positionRow('BTC');
    expect(within(region).queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows a failed first read as the section error, with a retry that works', async () => {
    const { user, accounting } = openDashboard({
      before: ({ accounting: fake }) => {
        fake.fail(() => problem(503, 'Service Unavailable', 'The accounting snapshot is locked.'));
      },
    });

    const region = await loadedRegion();
    const alert = await within(region).findByRole('alert');
    expect(within(alert).getByRole('heading', { level: 3 })).toHaveTextContent(
      'Could not load invested per asset',
    );
    expect(alert).toHaveTextContent('The accounting snapshot is locked.');
    // A failure is neither an empty state nor a zero.
    expect(region.querySelector('data')).toBeNull();
    expect(within(region).queryByRole('heading', { name: /No positions|No trades/ })).toBeNull();
    // The value section is untouched.
    expect(dataValues(await screen.findByRole('region', { name: 'Total value' }))).toContain(
      HEALTHY.total,
    );

    accounting.fail(null);
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    await positionRow('BTC');
    expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says the backend could not be reached when the first read never arrives', async () => {
    openDashboard({ overrides: [http.get(POSITIONS_PATH, () => HttpResponse.error())] });

    const alert = await within(await loadedRegion()).findByRole('alert');
    expect(alert).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('treats a truncated response as the section error, not a crash', async () => {
    openDashboard({
      overrides: [
        http.get(
          POSITIONS_PATH,
          () =>
            new HttpResponse('{"method":"weighted_average","positions":[{"asset":"BT', {
              status: 200,
              headers: { 'content-type': 'application/json' },
            }),
        ),
      ],
    });

    const alert = await within(await loadedRegion()).findByRole('alert');
    expect(within(alert).getByRole('heading')).toHaveTextContent(
      'Could not load invested per asset',
    );
    expect(alert).not.toHaveTextContent(/unexpected (end|token)|syntaxerror/i);
  });

  it('ignores fields the backend adds later', async () => {
    const response = investedPortfolio();
    openDashboard({
      overrides: [
        http.get(POSITIONS_PATH, () =>
          HttpResponse.json({
            ...response,
            ...{ schema_version: 2 },
            positions: response.positions.map((entry) => ({ ...entry, ...{ lots: 3 } })),
          }),
        ),
      ],
    });

    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
    expect(await loadedRegion()).not.toHaveTextContent(/schema_version|lots/);
  });

  it('keeps the figures on screen when a poll fails, says why, and clears it on the next', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const { accounting } = openDashboard();
    await positionRow('BTC');

    accounting.fail(() => problem(503, 'Service Unavailable', 'The database is restarting.'));
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    const region = await loadedRegion();
    const notice = await within(region).findByRole('alert');
    expect(notice).toHaveTextContent(
      'Could not refresh invested per asset: The database is restarting. Showing what was last loaded.',
    );
    expect(notice.textContent).not.toMatch(/\.\s*\./);
    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
    expect(within(region).queryByRole('heading', { name: /Could not load/ })).toBeNull();

    accounting.fail(null);
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    await waitFor(() => {
      expect(within(region).queryByRole('alert')).not.toBeInTheDocument();
    });
    await positionRow('BTC');
  });

  it('says the server could not be reached when a poll never arrives', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const { accounting } = openDashboard();
    await positionRow('BTC');

    accounting.fail(() => HttpResponse.error());
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    expect(await within(await loadedRegion()).findByRole('alert')).toHaveTextContent(
      'Could not refresh invested per asset: The server could not be reached. Showing what was last loaded.',
    );
    await positionRow('BTC');
  });

  it('picks up a new snapshot on its next poll, without a reload', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const { accounting } = openDashboard({ positions: stablecoinOnlySnapshot() });
    await within(await loadedRegion()).findByRole('heading', { name: 'No positions' });

    accounting.setPositions(investedPortfolio());
    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    await positionRow('BTC');
  });

  it('a 401 on the positions read returns to the login page', async () => {
    openDashboard({
      overrides: [
        http.get(POSITIONS_PATH, () => problem(401, 'Unauthorized', 'Authentication is required.')),
      ],
    });

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
  });
});

describe('InvestedSection: beside the value section', () => {
  it('shows the invested figures to an owner with trades and no wallets', async () => {
    // The reason the value section's early returns became its own states: before, "no
    // wallets" was the whole page, and an owner who only trades never saw what went in.
    openDashboard({ portfolio: emptyPortfolio() });

    expect(await screen.findByRole('heading', { name: /no wallets yet/i })).toBeInTheDocument();
    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
  });

  it('shows the invested figures when the balances cannot be read', async () => {
    openDashboard({
      overrides: [
        http.get(BALANCES_CURRENT_PATH, () =>
          problem(503, 'Service Unavailable', 'The database is not reachable.'),
        ),
      ],
    });

    expect(
      await screen.findByRole('heading', { name: /could not load your portfolio/i }),
    ).toBeInTheDocument();
    await positionRow('BTC');
    expect(within(await loadedRegion()).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('comes after the value section', async () => {
    openDashboard();

    const total = await screen.findByRole('region', { name: 'Total value' });
    const invested = await loadedRegion();
    expect(total.compareDocumentPosition(invested) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('asks for the exchange list the exchanges page uses, and nothing with an address', async () => {
    const { accounting, exchanges } = openDashboard();
    await positionRow('BTC');

    expect(accounting.requests.map((entry) => new URL(entry.url).pathname)).toContain(
      POSITIONS_PATH,
    );
    expect(new URL(accounting.requests[0]?.url ?? '').search).toBe('');
    expect(exchanges.requestsTo('list').map((entry) => new URL(entry.url).pathname)).toContain(
      EXCHANGES_PATH,
    );
  });
});

describe('InvestedSection: after an exchange sync', () => {
  it('shows the snapshot the sync recomputed as soon as the owner returns to the dashboard', async () => {
    // Witness for the `['accounting']` invalidation. The clock is frozen, so the positions
    // cached before the sync are still fresh by `staleTime` on return, and without the
    // invalidation they would be served as they were: "No positions".
    let accounting: FakeAccounting | undefined;
    const { user } = openDashboard({
      positions: stablecoinOnlySnapshot(),
      before: (fakes) => {
        accounting = fakes.accounting;
      },
      onExchangeSync: (fake) => {
        // The run stores fills, and the server recomputes before it answers.
        accounting?.setPositions(investedPortfolio());
        const run = finishedRun({
          run_id: 9,
          trigger: 'manual',
          started_at: '2026-09-24T11:59:00.000000Z',
          accounts: [accountSucceeded('bitget', { fills_seen: 3, fills_inserted: 3 })],
        });
        fake.setRuns([run]);
        return syncTriggered(run, false);
      },
    });
    await within(await loadedRegion()).findByRole('heading', { name: 'No positions' });

    const nav = screen.getByRole('navigation', { name: 'Main' });
    await user.click(within(nav).getByRole('link', { name: 'Exchanges' }));
    await user.click(await screen.findByRole('button', { name: 'Sync now' }));
    await screen.findByText(/^The sync /);

    await user.click(within(nav).getByRole('link', { name: 'Dashboard' }));

    expect(dataValues(cell(await positionRow('BTC'), 'Invested (USD)'))).toEqual([
      '52500.000000000000000000',
    ]);
  });
});
