import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  INTERRUPTED_MESSAGE,
  NEVER_SYNCED_MESSAGE,
  NOT_COVERED_MESSAGE,
  SYNC_ERROR_MESSAGES,
  UNKNOWN_FAILURE_MESSAGE,
} from '@/lib/freshness';
import { PRICE_UNAVAILABLE_MESSAGES } from '@/lib/prices';
import {
  BALANCES_CURRENT_PATH,
  BALANCES_RUNS_PATH,
  BALANCES_SYNC_PATH,
  fakePortfolio,
  WALLETS_PATH,
  type FakePortfolio,
  type FakePortfolioOptions,
} from '@/test/fakePortfolio';
import {
  ADDRESSES,
  BTC_OBSERVED_AT,
  chainOutcome,
  currentBalances,
  emptyPortfolio,
  failedOutcome,
  HEALTHY,
  healthyPortfolio,
  HUGE_KAS_QUANTITY,
  interruptedRun,
  KAS_OBSERVED_AT,
  kaspaDownPortfolio,
  NOW,
  previousRun,
  PREVIOUS_OBSERVED_AT,
  price,
  RUN_FINISHED_AT,
  RUN_STARTED_AT,
  runningRun,
  STALE_PRICE_AS_OF,
  syncRun,
  triggered,
  unreadBalance,
  unreadEntry,
  wallet,
  walletBalance,
  type CurrentBalancesResponse,
  type PortfolioScenario,
  type PriceUnavailable,
  type SyncErrorKind,
} from '@/test/fixtures';
import { currentPath, renderApp, settle } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';

/**
 * Every dashboard test runs under a fixed clock, faking `Date` only.
 *
 * `setTimeout` stays real: MSW resolves responses through it, and so do
 * `settle()` and `waitFor`'s own timeout. Faking it stalls every request.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

interface Setup {
  readonly user: UserEvent;
  readonly fake: FakePortfolio;
}

/**
 * Signs in, installs the fake backend and opens the dashboard. `overrides`
 * take precedence over the fake and are in place before the first render.
 */
function openDashboard(
  options: FakePortfolioOptions = healthyPortfolio(),
  overrides: readonly HttpHandler[] = [],
): Setup {
  const user = userEvent.setup();
  const fake = fakePortfolio(options);
  server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers, ...fake.handlers);
  server.use(...overrides);

  renderApp(['/']);

  return { user, fake };
}

async function totalRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Total value' });
}

async function assetsRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Assets' });
}

async function walletsRegion(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Wallets' });
}

/** The asset table row whose row header is `symbol`. */
async function assetRow(symbol: string): Promise<HTMLTableRowElement> {
  const header = within(await assetsRegion()).getByRole('rowheader', { name: symbol });
  const row = header.closest('tr');

  if (row === null) {
    throw new Error(`The ${symbol} row header is not inside a table row.`);
  }

  return row;
}

/**
 * The wallet table row that contains `text`, or whose `<Address>` carries
 * `text` as its title.
 */
async function walletRow(text: string): Promise<HTMLTableRowElement> {
  const region = await walletsRegion();
  const rows = within(region)
    .getAllByRole('row')
    .filter((row): row is HTMLTableRowElement => row instanceof HTMLTableRowElement)
    .filter((row) => row.closest('tbody') !== null);
  const match = rows.find(
    (row) =>
      within(row).queryByText(text, { exact: false }) !== null ||
      within(row).queryByTitle(text) !== null,
  );

  if (match === undefined) {
    throw new Error(`No wallet row contains "${text}". Table was: ${region.textContent}`);
  }

  return match;
}

/** The cell of `row` under the column headed `column`. */
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

/**
 * A standalone zero amount in rendered text: `0`, `0.00`, `+0`, `-0.0`, as a
 * word of its own. It does not match the zero inside `0.08`, `10`, `w508` or
 * a truncated address, which is what makes it usable on a whole row.
 */
const ZERO_AMOUNT = /(?:^|[^\w.,])[-+]?0(?:\.0+)?(?![\w.,])/;

/**
 * The "no silent zero" assertion, on the element rather than on the page: no
 * `<data>` inside it carries a zero, and no text inside it reads as one.
 */
function expectNoRenderedZero(element: HTMLElement): void {
  for (const value of dataValues(element)) {
    expect(value).not.toMatch(/^-?0(?:\.0+)?$/);
  }
  expect(element.textContent).not.toMatch(ZERO_AMOUNT);
}

/** A cell that renders "—" and no amount at all. */
function expectDash(element: HTMLElement): void {
  expect(element.textContent.trim()).toBe('—');
  expect(element.querySelector('data')).toBeNull();
}

/** The "last updated" line. */
async function lastUpdated(): Promise<HTMLElement> {
  return screen.findByText(/balances as of/i, { selector: 'p' });
}

/** Waits until the dashboard has rendered its data. */
async function loaded(): Promise<void> {
  await totalRegion();
  await walletsRegion();
}

/** The healthy portfolio, with one wallet row replaced. */
function withWalletRow(
  scenario: PortfolioScenario,
  walletId: number,
  patch: Partial<CurrentBalancesResponse['wallets'][number]>,
): PortfolioScenario {
  return {
    ...scenario,
    current: {
      ...scenario.current,
      wallets: scenario.current.wallets.map((row) =>
        row.wallet_id === walletId ? { ...row, ...patch } : row,
      ),
    },
  };
}

describe('DashboardPage: values', () => {
  it("renders the total, each wallet's value and each asset's quantity and value", async () => {
    openDashboard();

    const total = await totalRegion();
    expect(dataValues(total)).toContain(HEALTHY.total);
    expect(total).toHaveTextContent('2,296,084,419.75');
    expect(total).toHaveTextContent('EUR');
    expect(total).not.toHaveTextContent(/partial/i);

    const btc = await assetRow('BTC');
    expect(dataValues(cell(btc, 'Quantity'))).toEqual([HEALTHY.btcQuantitySum]);
    expect(dataValues(cell(btc, 'Price'))).toEqual(['52000.00']);
    expect(dataValues(cell(btc, 'Value'))).toEqual([HEALTHY.btcValueSum]);
    expect(cell(btc, 'Value')).toHaveTextContent('84,419.75 EUR');

    const kas = await assetRow('KAS');
    expect(dataValues(cell(kas, 'Quantity'))).toEqual([HUGE_KAS_QUANTITY]);
    expect(dataValues(cell(kas, 'Price'))).toEqual(['0.08']);
    expect(dataValues(cell(kas, 'Value'))).toEqual([HEALTHY.kasValue]);

    const cold = await walletRow('Cold storage');
    expect(dataValues(cell(cold, 'Quantity'))).toEqual(['1.50000000']);
    expect(dataValues(cell(cold, 'Value'))).toEqual(['78000.0000000000']);
    expect(cell(cold, 'Value')).toHaveTextContent('78,000.00 EUR');

    const spending = await walletRow('Spending');
    expect(dataValues(cell(spending, 'Quantity'))).toEqual(['0.12345678']);
    expect(dataValues(cell(spending, 'Value'))).toEqual(['6419.7525600000']);

    // The Kaspa wallet has no label: it is recognised by its address.
    const unlabelled = await walletRow(ADDRESSES.kasPrimary);
    expect(unlabelled).toHaveTextContent('Kaspa');
    expect(dataValues(cell(unlabelled, 'Value'))).toEqual([HEALTHY.kasValue]);

    for (const row of [cold, spending, unlabelled]) {
      expect(cell(row, 'Freshness')).toHaveTextContent(/up to date/i);
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('asks for the backend default currency and renders the one that comes back', async () => {
    const scenario = healthyPortfolio();
    const { fake } = openDashboard({
      ...scenario,
      current: { ...scenario.current, quote_currency: 'USD' },
    });

    const total = await totalRegion();
    expect(total).toHaveTextContent('USD');
    expect(total).not.toHaveTextContent('EUR');

    const currentReads = fake.requests.filter(
      (entry) => new URL(entry.url).pathname === BALANCES_CURRENT_PATH,
    );
    expect(currentReads.length).toBeGreaterThan(0);
    // No currency is chosen on the client; the backend's default stands.
    expect(new URL(currentReads[0]?.url ?? '').search).toBe('');
  });

  it('an asset held in two wallets shows the sum of both', async () => {
    // 0.1 + 0.2 is the canonical IEEE-754 failure: 0.30000000000000004.
    // Worked by hand: quantity 0.10000000 + 0.20000000 = 0.3; value
    // 5200.0100000000 + 10400.0200000000 = 15600.03 at a price of 52000.10.
    const btcPrice = price({ amount: '52000.10' });
    openDashboard({
      wallets: [
        wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'First' }),
        wallet({ id: 2, address: ADDRESSES.btcLegacy, label: 'Second' }),
      ],
      current: currentBalances({
        total: '15600.0300000000',
        as_of: BTC_OBSERVED_AT,
        wallets: [
          walletBalance({
            wallet_id: 1,
            label: 'First',
            confirmed: '10000000',
            quantity: '0.10000000',
            value: '5200.0100000000',
            price: btcPrice,
          }),
          walletBalance({
            wallet_id: 2,
            label: 'Second',
            confirmed: '20000000',
            quantity: '0.20000000',
            value: '10400.0200000000',
            price: btcPrice,
          }),
        ],
      }),
      runs: [syncRun(), previousRun()],
    });

    const btc = await assetRow('BTC');
    const [quantity] = dataValues(cell(btc, 'Quantity'));
    const [value] = dataValues(cell(btc, 'Value'));

    expect(quantity).not.toContain('0000000000004');
    expect(quantity?.replace(/0+$/, '')).toBe('0.3');
    expect(value?.replace(/0+$/, '')).toBe('15600.03');
    expect(cell(btc, 'Value')).toHaveTextContent('15,600.03 EUR');
    // One asset row, not one per wallet.
    expect(within(await assetsRegion()).getAllByRole('rowheader')).toHaveLength(1);
  });

  it('lists each asset once, whatever order the wallets come in', async () => {
    const scenario = healthyPortfolio();
    const [first, second, third] = scenario.current.wallets;
    openDashboard({
      ...scenario,
      current: {
        ...scenario.current,
        wallets: [third, first, second].filter((row) => row !== undefined),
      },
    });

    const headers = within(await assetsRegion()).getAllByRole('rowheader');
    expect(headers.map((header) => header.textContent)).toEqual(['BTC', 'KAS']);
    expect(dataValues(cell(await assetRow('BTC'), 'Quantity'))).toEqual([HEALTHY.btcQuantitySum]);
  });
});

describe('DashboardPage: precision', () => {
  it('a balance past MAX_SAFE_INTEGER base units renders exactly', async () => {
    // 2870000000000000123 sompi. Through a JavaScript number the trailing 123
    // is lost; the <data value> attribute is where that would show.
    openDashboard();

    const kasRow = await walletRow(ADDRESSES.kasPrimary);
    expect(dataValues(cell(kasRow, 'Quantity'))).toEqual(['28700000000.00000123']);
    expect(cell(kasRow, 'Quantity')).toHaveTextContent('28,700,000,000.00000123 KAS');

    const kasAsset = await assetRow('KAS');
    expect(dataValues(cell(kasAsset, 'Quantity'))).toEqual(['28700000000.00000123']);
  });

  it('a non-zero pending renders as a signed amount', async () => {
    let scenario = withWalletRow(healthyPortfolio(), 1, { pending: '12000' });
    scenario = withWalletRow(scenario, 2, { pending: '-150000' });
    openDashboard(scenario);

    const cold = await walletRow('Cold storage');
    expect(cell(cold, 'Quantity')).toHaveTextContent('+0.00012 BTC pending');

    // An outgoing unconfirmed transaction keeps its minus sign.
    const spending = await walletRow('Spending');
    expect(cell(spending, 'Quantity')).toHaveTextContent('-0.0015 BTC pending');
    expect(cell(spending, 'Quantity')).not.toHaveTextContent('+-');

    // The quantity is still `confirmed` alone: pending is shown beside it,
    // never folded into it.
    expect(dataValues(cell(cold, 'Quantity'))).toContain('1.50000000');
  });

  it('a pending amount carries its exact value, like every other amount', async () => {
    openDashboard(withWalletRow(healthyPortfolio(), 1, { pending: '12000' }));

    const cold = await walletRow('Cold storage');
    const values = dataValues(cell(cold, 'Quantity'));
    expect(values).toHaveLength(2);
    expect(values[1]?.replace(/0+$/, '')).toBe('0.00012');
  });

  it('a pending amount past MAX_SAFE_INTEGER base units keeps every digit', async () => {
    openDashboard(withWalletRow(healthyPortfolio(), 1, { pending: '9007199254740993' }));

    const cold = await walletRow('Cold storage');
    // 9007199254740993 is the first integer a double cannot hold; as a number
    // it reads ...992.
    expect(cell(cold, 'Quantity')).toHaveTextContent('+90,071,992.54740993 BTC pending');
  });

  it.each([
    ['zero', '0'],
    ['null', null],
  ])('a %s pending renders nothing', async (_label, pending) => {
    openDashboard(withWalletRow(healthyPortfolio(), 1, { pending }));

    const cold = await walletRow('Cold storage');
    expect(cold).not.toHaveTextContent(/pending/i);
    expectNoRenderedZero(cold);
  });
});

describe('DashboardPage: last updated', () => {
  it('shows balances-as-of and the last sync, relative to now', async () => {
    openDashboard();
    await loaded();

    const line = await lastUpdated();
    // as_of is Kaspa's reading at 11:42, eighteen minutes before noon; the run
    // finished at 11:45, fifteen minutes before.
    expect(line).toHaveTextContent(/balances as of 18 minutes ago/i);
    expect(line).toHaveTextContent(/last sync succeeded 15 minutes ago/i);

    const times = Array.from(line.querySelectorAll('time'));
    expect(times.map((time) => time.getAttribute('datetime'))).toEqual([
      KAS_OBSERVED_AT,
      RUN_FINISHED_AT,
    ]);
    // The exact instant is one hover away.
    for (const time of times) {
      expect(time.getAttribute('title')).toMatch(/2026/);
    }
  });

  it('dates an interrupted run by when it started', async () => {
    const scenario = healthyPortfolio();
    openDashboard({
      ...scenario,
      runs: [
        interruptedRun({ chains: [chainOutcome({ chain_key: 'bitcoin', wallets_read: 2 })] }),
        previousRun(),
      ],
    });
    await loaded();

    const line = await lastUpdated();
    expect(line).toHaveTextContent(/last sync was interrupted 20 minutes ago/i);
    expect(
      Array.from(line.querySelectorAll('time')).map((time) => time.getAttribute('datetime')),
    ).toContain(RUN_STARTED_AT);
  });

  it.each([
    ['partial', /partially/i],
    ['failed', /failed/i],
  ] as const)('says a %s run was not a success', async (status, pattern) => {
    const scenario = healthyPortfolio();
    openDashboard({ ...scenario, runs: [syncRun({ status }), previousRun()] });
    await loaded();

    const line = await lastUpdated();
    expect(line).toHaveTextContent(pattern);
    expect(line).not.toHaveTextContent(/last sync succeeded/i);
  });

  it('says no sync has run yet when the run log is empty', async () => {
    const scenario = healthyPortfolio();
    openDashboard({ ...scenario, runs: [] });
    await loaded();

    expect(await lastUpdated()).toHaveTextContent(NEVER_SYNCED_MESSAGE);
  });

  it('says balances have never been read rather than giving a time', async () => {
    const wallets = [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'New' })];
    openDashboard({ wallets, runs: [] });
    await loaded();

    const line = await lastUpdated();
    expect(line).toHaveTextContent(/never/i);
    expect(line.querySelector('time')).toBeNull();
  });

  it('the relative time advances without a reload', async () => {
    // `useNow` re-renders on a `setInterval` tick, so the tick has to fire for
    // the text to move. Faking `setInterval` along with `Date` lets one
    // `advanceTimersByTime` move the clock and fire the tick together, which is
    // exactly what thirty real seconds would do. `setTimeout` stays real, so
    // MSW's responses, `settle()` and `waitFor`'s timeout keep working.
    //
    // The balance queries' 60-second polling runs on `setInterval` as well, so
    // advancing a minute also refetches - the same data, from the same fake.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    openDashboard();
    await loaded();

    const line = await lastUpdated();
    expect(line).toHaveTextContent(/balances as of 18 minutes ago/i);
    expect(line).toHaveTextContent(/15 minutes ago/i);

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    await waitFor(() => {
      expect(line).toHaveTextContent(/balances as of 19 minutes ago/i);
    });
    expect(line).toHaveTextContent(/last sync succeeded 16 minutes ago/i);

    act(() => {
      vi.advanceTimersByTime(60 * 60_000);
    });

    await waitFor(() => {
      expect(line).toHaveTextContent(/balances as of 1 hour ago/i);
    });
  });

  it('picks up the next scheduled run without a reload', async () => {
    // Both balance queries poll every minute. Without it, a dashboard left
    // open never shows the scheduler's next run.
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    const scenario = healthyPortfolio();
    const { fake } = openDashboard(scenario);
    await loaded();
    expect(await lastUpdated()).toHaveTextContent(/last sync succeeded 15 minutes ago/i);

    // The scheduler runs at 12:00:30 and reads Bitcoin wallet 1 at 2 BTC.
    const nextRunAt = '2026-09-24T12:00:30.000Z';
    fake.setRuns([
      syncRun({ run_id: 9, started_at: nextRunAt, finished_at: nextRunAt }),
      syncRun(),
    ]);
    fake.setCurrent({
      ...scenario.current,
      wallets: scenario.current.wallets.map((row) =>
        row.wallet_id === 1
          ? {
              ...row,
              confirmed: '200000000',
              quantity: '2.00000000',
              value: '104000.0000000000',
              observed_at: nextRunAt,
            }
          : row,
      ),
    });

    act(() => {
      vi.advanceTimersByTime(60_000);
    });

    const cold = await walletRow('Cold storage');
    await waitFor(() => {
      expect(dataValues(cell(cold, 'Quantity'))).toEqual(['2.00000000']);
    });
    expect(await lastUpdated()).toHaveTextContent(/last sync succeeded just now/i);
  });
});

describe('DashboardPage: refresh', () => {
  it('refresh posts a sync and re-reads the balances', async () => {
    const scenario = healthyPortfolio();
    const manualRunAt = '2026-09-24T11:59:50.000Z';
    const { user, fake } = openDashboard({
      ...scenario,
      onSync: (portfolio) => {
        const run = syncRun({
          run_id: 9,
          trigger: 'manual',
          started_at: manualRunAt,
          finished_at: manualRunAt,
        });
        portfolio.setRuns([run, syncRun()]);
        portfolio.setCurrent({
          ...scenario.current,
          wallets: scenario.current.wallets.map((row) =>
            row.wallet_id === 1
              ? {
                  ...row,
                  confirmed: '200000000',
                  quantity: '2.00000000',
                  value: '104000.0000000000',
                  observed_at: manualRunAt,
                }
              : row,
          ),
        });
        return triggered(run);
      },
    });
    await loaded();
    const readsBefore = fake.requests.filter(
      (entry) => new URL(entry.url).pathname === BALANCES_CURRENT_PATH,
    ).length;

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    const cold = await walletRow('Cold storage');
    await waitFor(() => {
      expect(dataValues(cell(cold, 'Quantity'))).toEqual(['2.00000000']);
    });

    const syncs = fake.writes('POST', BALANCES_SYNC_PATH);
    expect(syncs).toHaveLength(1);
    // A bodyless write still declares JSON, or the backend's guard refuses it.
    expect(syncs[0]?.contentType).toBe('application/json');
    expect(
      fake.requests.filter((entry) => new URL(entry.url).pathname === BALANCES_CURRENT_PATH).length,
    ).toBeGreaterThan(readsBefore);
    // The run log is re-read too, so the status line moves with the numbers.
    expect(await lastUpdated()).toHaveTextContent(/last sync succeeded just now/i);
  });

  it('refresh is disabled while a sync is in flight', async () => {
    const { user, fake } = openDashboard();
    await loaded();
    const release = fake.hold('sync');
    const refresh = screen.getByRole('button', { name: 'Refresh' });

    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    await user.click(refresh);

    await waitFor(() => {
      expect(refresh).toBeDisabled();
    });
    // A sync can take tens of seconds; a disabled button alone is silence.
    expect(await screen.findByRole('status')).toHaveTextContent(/sync|refresh/i);
    await user.click(refresh);
    expect(fake.writes('POST', BALANCES_SYNC_PATH)).toHaveLength(1);

    release();

    await waitFor(() => {
      expect(refresh).toBeEnabled();
    });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(fake.writes('POST', BALANCES_SYNC_PATH)).toHaveLength(1);
  });

  it('a failed refresh keeps the balances on screen and says why', async () => {
    const { user } = openDashboard(healthyPortfolio(), [
      http.post(BALANCES_SYNC_PATH, () =>
        problem(500, 'Internal Server Error', 'The server encountered an unexpected condition.'),
      ),
    ]);
    await loaded();

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/refresh failed/i);
    expect(alert).toHaveTextContent('The server encountered an unexpected condition.');
    // The data already on screen stays.
    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
    expect(dataValues(cell(await walletRow('Cold storage'), 'Value'))).toEqual([
      '78000.0000000000',
    ]);
    // And the owner can try again.
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('a refresh cut off by a proxy does not claim the sync did not run', async () => {
    // The backend holds the request for the whole run, and shields the run
    // from a dropped connection. When a proxy gives up first, the sync is
    // still going, so "could not start" would be false.
    const { user } = openDashboard(healthyPortfolio(), [
      http.post(
        BALANCES_SYNC_PATH,
        () =>
          new HttpResponse('<html><body>504 Gateway Time-out</body></html>', {
            status: 504,
            statusText: 'Gateway Timeout',
            headers: { 'content-type': 'text/html' },
          }),
      ),
    ]);
    await loaded();

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/refresh failed/i);
    expect(alert).not.toHaveTextContent(/gateway/i);
    expect(alert).not.toHaveTextContent(/could not .*start|did not (start|run)|was not started/i);
    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
  });
});

describe('DashboardPage: stale prices and provider failures', () => {
  it('a stale price is labelled stale on the asset and wallet rows', async () => {
    const stale = price({ as_of: STALE_PRICE_AS_OF, stale: true });
    let scenario = withWalletRow(healthyPortfolio(), 1, { price: stale });
    scenario = withWalletRow(scenario, 2, { price: stale });
    openDashboard(scenario);

    const btc = await assetRow('BTC');
    expect(cell(btc, 'Price')).toHaveTextContent(/stale/i);
    // Its age: priced at 10:00, two hours before noon.
    expect(cell(btc, 'Price')).toHaveTextContent(/2 hours ago/i);

    for (const label of ['Cold storage', 'Spending']) {
      const row = await walletRow(label);
      expect(row).toHaveTextContent(/stale/i);
      expect(row).toHaveTextContent(/2 hours ago/i);
      // Still valued: a stale price is information, not a missing one.
      expect(dataValues(cell(row, 'Value')).length).toBe(1);
    }

    // The Kaspa price is fresh, and says nothing about staleness.
    expect(cell(await assetRow('KAS'), 'Price')).not.toHaveTextContent(/stale/i);
    expect(await walletRow(ADDRESSES.kasPrimary)).not.toHaveTextContent(/stale/i);

    expect(await totalRegion()).toHaveTextContent(/stale price/i);
  });

  it('a fresh price says nothing about staleness', async () => {
    openDashboard();

    expect(await totalRegion()).not.toHaveTextContent(/stale/i);
    expect(await assetsRegion()).not.toHaveTextContent(/stale/i);
    expect(await walletsRegion()).not.toHaveTextContent(/stale/i);
  });

  const REASONS: PriceUnavailable[] = [
    'never_fetched',
    'every_source_failed',
    'unsupported_pair',
    'no_source_configured',
  ];

  /** The healthy portfolio with Kaspa unpriced for `reason`. */
  function kaspaUnpriced(reason: PriceUnavailable): PortfolioScenario {
    const scenario = withWalletRow(healthyPortfolio(), 3, { value: null, price: null });
    return {
      ...scenario,
      current: {
        ...scenario.current,
        // The backend's own sum over the rows that have a value.
        total: '84419.7525600000',
        complete: false,
        unpriced: [{ asset_symbol: 'KAS', quantity: HUGE_KAS_QUANTITY, reason }],
      },
    };
  }

  it.each(REASONS)('an unpriced asset shows its reason and no value (%s)', async (reason) => {
    openDashboard(kaspaUnpriced(reason));

    const kas = await assetRow('KAS');
    expect(cell(kas, 'Price')).toHaveTextContent(PRICE_UNAVAILABLE_MESSAGES[reason]);
    expectDash(cell(kas, 'Value'));
    // The quantity is known; only its value is not.
    expect(dataValues(cell(kas, 'Quantity'))).toEqual([HUGE_KAS_QUANTITY]);

    const total = await totalRegion();
    expect(total).toHaveTextContent(/partial/i);
    expect(total).toHaveTextContent('KAS');
    expect(dataValues(total)).toContain('84419.7525600000');
  });

  it('a wallet with no value renders no zero', async () => {
    openDashboard(kaspaUnpriced('every_source_failed'));

    const row = await walletRow(ADDRESSES.kasPrimary);
    expectDash(cell(row, 'Value'));
    expectNoRenderedZero(row);
    // The reading itself is still there.
    expect(dataValues(cell(row, 'Quantity'))).toEqual([HUGE_KAS_QUANTITY]);

    const kas = await assetRow('KAS');
    expectNoRenderedZero(cell(kas, 'Value'));
    expectNoRenderedZero(cell(kas, 'Price'));
  });

  /** The healthy portfolio plus a fourth wallet, registered after the latest run. */
  function withUnreadBitcoinWallet(): PortfolioScenario {
    const scenario = healthyPortfolio();
    const added = wallet({ id: 4, address: ADDRESSES.btcScript, label: 'Brand new' });

    return {
      ...scenario,
      wallets: [...scenario.wallets, added],
      current: {
        ...scenario.current,
        complete: false,
        // The backend fills in the asset's price on an unread row too.
        wallets: [...scenario.current.wallets, unreadBalance(added, price())],
        unread: [unreadEntry(added)],
      },
    };
  }

  it('an unread wallet renders no zero', async () => {
    openDashboard(withUnreadBitcoinWallet());

    const row = await walletRow('Brand new');
    expect(row).toHaveTextContent(/not read yet/i);
    expectDash(cell(row, 'Value'));
    expect(row.querySelector('data')).toBeNull();
    expectNoRenderedZero(row);

    const total = await totalRegion();
    expect(total).toHaveTextContent(/partial/i);
    expect(total).toHaveTextContent(/1 wallet not yet read/i);
    // The total itself is the backend's, unchanged.
    expect(dataValues(total)).toContain(HEALTHY.total);
  });

  it('an asset whose sum excludes two unread wallets says so in the plural', async () => {
    const scenario = withUnreadBitcoinWallet();
    const second = wallet({ id: 5, address: ADDRESSES.btcRegtest, label: 'Also new' });
    openDashboard({
      ...scenario,
      wallets: [...scenario.wallets, second],
      current: {
        ...scenario.current,
        wallets: [...scenario.current.wallets, unreadBalance(second, price())],
        unread: [...scenario.current.unread, unreadEntry(second)],
      },
    });

    const btc = await assetRow('BTC');
    expect(cell(btc, 'Quantity')).toHaveTextContent(/excludes 2 wallets not yet read/i);
    expect(await totalRegion()).toHaveTextContent(/2 wallets not yet read/i);
  });

  it('an asset with no value and no stated reason still renders no zero', async () => {
    // The backend's two lists disagree: Kaspa has no value, yet `unpriced` does
    // not name it. The page cannot explain what the backend did not, and it
    // must not paper over the gap with a zero either.
    openDashboard(withWalletRow(healthyPortfolio(), 3, { value: null, price: null }));

    const kas = await assetRow('KAS');
    expectDash(cell(kas, 'Price'));
    expectDash(cell(kas, 'Value'));
    expect(dataValues(cell(kas, 'Quantity'))).toEqual([HUGE_KAS_QUANTITY]);
    expectDash(cell(await walletRow(ADDRESSES.kasPrimary), 'Value'));
  });

  it('an asset whose sum excludes an unread wallet says so', async () => {
    openDashboard(withUnreadBitcoinWallet());

    const btc = await assetRow('BTC');
    // Still the sum of the two wallets that were read.
    expect(dataValues(cell(btc, 'Quantity'))).toEqual([HEALTHY.btcQuantitySum]);
    expect(cell(btc, 'Quantity')).toHaveTextContent(/excludes 1 wallet not yet read/i);
    expect(cell(await assetRow('KAS'), 'Quantity')).not.toHaveTextContent(/excludes/i);
  });

  it('an asset whose every wallet is unread says so and renders no zero', async () => {
    const wallets = [
      wallet({ id: 1, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Fresh install' }),
      wallet({ id: 2, chain_key: 'kaspa', address: ADDRESSES.kasSecondary, label: 'Second' }),
    ];
    // No `current` given: the fake derives what the backend sends for wallets
    // no run has read - nulls throughout, a total of "0", complete false.
    openDashboard({ wallets, runs: [] });

    const kas = await assetRow('KAS');
    expect(kas).toHaveTextContent(/not read yet/i);
    expectDash(cell(kas, 'Value'));
    expectNoRenderedZero(cell(kas, 'Quantity'));
    expectNoRenderedZero(cell(kas, 'Price'));

    for (const label of ['Fresh install', 'Second']) {
      const row = await walletRow(label);
      expect(row).toHaveTextContent(/not read yet/i);
      expectNoRenderedZero(row);
    }

    const total = await totalRegion();
    expect(total).toHaveTextContent(/partial/i);
    expect(total).toHaveTextContent(/2 wallets not yet read/i);
  });

  it('an unread wallet on a failed chain says why it has not been read', async () => {
    const wallets = [
      wallet({ id: 1, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Never read' }),
    ];
    openDashboard({
      wallets,
      runs: [syncRun({ status: 'failed', chains: [failedOutcome('kaspa', 'rate_limited')] })],
    });

    const row = await walletRow('Never read');
    expect(row).toHaveTextContent(/not read yet/i);
    expect(row).toHaveTextContent(SYNC_ERROR_MESSAGES.rate_limited);
    expectNoRenderedZero(row);
  });

  it('an unread wallet on a chain that failed for no stated reason still says it failed', async () => {
    const wallets = [
      wallet({ id: 1, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Never read' }),
    ];
    openDashboard({
      wallets,
      runs: [
        syncRun({
          status: 'failed',
          chains: [chainOutcome({ chain_key: 'kaspa', status: 'failed', error_kind: null })],
        }),
      ],
    });

    const row = await walletRow('Never read');
    expect(row).toHaveTextContent(/not read yet/i);
    expect(row).toHaveTextContent(UNKNOWN_FAILURE_MESSAGE);
  });

  it('an unread wallet on a chain that succeeded gives no failure reason', async () => {
    // Registered after the run read its chain: nothing failed, it is simply new.
    openDashboard(withUnreadBitcoinWallet());

    const row = await walletRow('Brand new');
    expect(row).toHaveTextContent(/not read yet/i);
    for (const sentence of [...Object.values(SYNC_ERROR_MESSAGES), UNKNOWN_FAILURE_MESSAGE]) {
      expect(row).not.toHaveTextContent(sentence);
    }
  });

  it.each([
    'unavailable',
    'rate_limited',
    'response',
    'unknown_chain',
    'address_rejected',
    'internal',
  ] as SyncErrorKind[])(
    'a wallet whose chain failed shows the reason and the age of its reading (%s)',
    async (errorKind) => {
      openDashboard(kaspaDownPortfolio(errorKind));

      const row = await walletRow(ADDRESSES.kasPrimary);
      const freshness = cell(row, 'Freshness');
      expect(freshness).toHaveTextContent(SYNC_ERROR_MESSAGES[errorKind]);
      // The reading is from the previous run, 35 minutes before noon.
      expect(freshness).toHaveTextContent(/showing the balance from 35 minutes ago/i);
      expect(freshness.querySelector('time')?.getAttribute('datetime')).toBe(PREVIOUS_OBSERVED_AT);
      expect(freshness).not.toHaveTextContent(/up to date/i);
      // The old reading is still a reading: it is shown, and labelled.
      expect(dataValues(cell(row, 'Value'))).toEqual([HEALTHY.kasValue]);
    },
  );

  it('a sync that was interrupted before a chain says so on that chain', async () => {
    const scenario = kaspaDownPortfolio();
    openDashboard({
      ...scenario,
      runs: [
        interruptedRun({ chains: [chainOutcome({ chain_key: 'bitcoin', wallets_read: 2 })] }),
        previousRun(),
      ],
    });

    expect(cell(await walletRow(ADDRESSES.kasPrimary), 'Freshness')).toHaveTextContent(
      INTERRUPTED_MESSAGE,
    );
    expect(cell(await walletRow('Cold storage'), 'Freshness')).toHaveTextContent(/up to date/i);
  });

  it('a restored wallet with an old reading is not called up to date', async () => {
    // Bitcoin succeeded in the latest run, which skipped this wallet because it
    // was archived at the time. Its reading is from the run before.
    openDashboard(withWalletRow(healthyPortfolio(), 1, { observed_at: PREVIOUS_OBSERVED_AT }));

    const cold = await walletRow('Cold storage');
    expect(cell(cold, 'Freshness')).toHaveTextContent(NOT_COVERED_MESSAGE);
    expect(cell(cold, 'Freshness')).not.toHaveTextContent(/up to date/i);
    expect(cell(await walletRow('Spending'), 'Freshness')).toHaveTextContent(/up to date/i);
  });
});

describe('DashboardPage: a total with nothing in it', () => {
  it('an incomplete total with no valued wallet renders no zero', async () => {
    // Every wallet unread: the backend sends `total: "0"` with `complete: false`.
    // "0.00 EUR - Partial" would still put a zero where the owner looks first.
    const wallets = [
      wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'First' }),
      wallet({ id: 2, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Second' }),
    ];
    openDashboard({ wallets, runs: [] });

    const total = await totalRegion();
    expectNoRenderedZero(total);
    expect(total.querySelector('data')).toBeNull();
    expect(total).toHaveTextContent(/—|not available yet/i);
    expect(total).toHaveTextContent(/2 wallets not yet read/i);
  });

  it('an incomplete total whose only asset is unpriced renders no zero', async () => {
    // Read, but nothing could be valued: still no number to show.
    const row = healthyPortfolio().current.wallets[2];
    if (row === undefined) {
      throw new Error('The healthy portfolio has no Kaspa row.');
    }
    openDashboard({
      wallets: [
        wallet({ id: 3, chain_key: 'kaspa', address: ADDRESSES.kasPrimary, label: 'Only' }),
      ],
      current: currentBalances({
        total: '0',
        complete: false,
        as_of: KAS_OBSERVED_AT,
        wallets: [{ ...row, label: 'Only', value: null, price: null }],
        unpriced: [{ asset_symbol: 'KAS', quantity: HUGE_KAS_QUANTITY, reason: 'never_fetched' }],
      }),
      runs: [syncRun(), previousRun()],
    });

    const total = await totalRegion();
    expectNoRenderedZero(total);
    expect(total.querySelector('data')).toBeNull();
    expect(total).toHaveTextContent('KAS');
  });

  it('a complete portfolio that is genuinely empty of funds still renders 0.00', async () => {
    // Every wallet read, every one holding nothing: zero is the true answer,
    // and hiding it would be the opposite mistake.
    openDashboard({
      wallets: [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'Emptied' })],
      current: currentBalances({
        total: '0.0000000000',
        complete: true,
        as_of: BTC_OBSERVED_AT,
        wallets: [
          walletBalance({
            wallet_id: 1,
            label: 'Emptied',
            confirmed: '0',
            quantity: '0.00000000',
            value: '0.0000000000',
          }),
        ],
      }),
      runs: [syncRun(), previousRun()],
    });

    const total = await totalRegion();
    expect(dataValues(total)).toEqual(['0.0000000000']);
    expect(total).toHaveTextContent('0.00 EUR');
    expect(total).not.toHaveTextContent(/partial|not available/i);
  });
});

describe('DashboardPage: partial failure', () => {
  it("one chain down: the other chain's rows are fresh, the failed chain's rows say so, and the total says what it includes", async () => {
    openDashboard(kaspaDownPortfolio('unavailable'));

    for (const label of ['Cold storage', 'Spending']) {
      expect(cell(await walletRow(label), 'Freshness')).toHaveTextContent(/up to date/i);
    }

    const kas = await walletRow(ADDRESSES.kasPrimary);
    expect(cell(kas, 'Freshness')).toHaveTextContent(SYNC_ERROR_MESSAGES.unavailable);
    expect(cell(kas, 'Freshness')).not.toHaveTextContent(/up to date/i);

    // The total still counts the Kaspa reading, so it has to say that part of
    // it is a balance the last sync could not refresh.
    const total = await totalRegion();
    expect(dataValues(total)).toContain(HEALTHY.total);
    expect(total).toHaveTextContent(/could not (be )?refresh/i);
    // Not the whole page: no whole-page error.
    expect(screen.queryByRole('heading', { name: /could not load/i })).not.toBeInTheDocument();
  });

  it('balances render when the runs request fails', async () => {
    openDashboard(healthyPortfolio(), [
      http.get(BALANCES_RUNS_PATH, () =>
        problem(500, 'Internal Server Error', 'The run log is locked.'),
      ),
    ]);

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(/sync status is unavailable/i);
    expect(notice).toHaveTextContent('The run log is locked.');

    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
    // With no run log, nothing can be called fresh.
    for (const label of ['Cold storage', 'Spending', ADDRESSES.kasPrimary]) {
      const row = await walletRow(label);
      expect(dataValues(cell(row, 'Value')).length).toBe(1);
      expect(cell(row, 'Freshness')).not.toHaveTextContent(/up to date/i);
    }
    expect(screen.queryByText(/up to date/i)).not.toBeInTheDocument();
  });

  it('balances render when the wallets request fails', async () => {
    openDashboard(healthyPortfolio(), [
      http.get(WALLETS_PATH, () =>
        problem(500, 'Internal Server Error', 'The wallet table is locked.'),
      ),
    ]);

    const notice = await screen.findByRole('alert');
    expect(notice).toHaveTextContent(/addresses are unavailable/i);

    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
    // Labelled rows keep their label; the unlabelled one falls back to chain and id.
    expect(await walletRow('Cold storage')).toBeInTheDocument();
    const fallback = await walletRow('Kaspa wallet #3');
    expect(dataValues(cell(fallback, 'Value'))).toEqual([HEALTHY.kasValue]);
    expect(
      within(await walletsRegion()).queryByRole('button', { name: 'Copy address' }),
    ).not.toBeInTheDocument();
  });

  it('the runs and wallets requests failing together still leave the balances', async () => {
    openDashboard(healthyPortfolio(), [
      http.get(BALANCES_RUNS_PATH, () => HttpResponse.error()),
      http.get(WALLETS_PATH, () => HttpResponse.error()),
    ]);

    await waitFor(() => {
      expect(screen.getAllByRole('alert')).toHaveLength(2);
    });
    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
    for (const alert of screen.getAllByRole('alert')) {
      expect(alert).not.toHaveTextContent(/failed to fetch/i);
    }
  });
});

describe('DashboardPage: states', () => {
  it('announces that the portfolio is loading', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openDashboard(healthyPortfolio(), [
      http.get(BALANCES_CURRENT_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);

    const status = await screen.findByText(/loading your portfolio/i);
    expect(status).toHaveAttribute('role', 'status');
    // No total and no zero while nothing has arrived.
    expect(screen.queryByRole('region', { name: 'Total value' })).not.toBeInTheDocument();
    expect(screen.queryByText(/no wallets yet/i)).not.toBeInTheDocument();

    release();

    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
  });

  it('no wallets: the dashboard links to the wallets page', async () => {
    const { user } = openDashboard(emptyPortfolio());

    const main = await screen.findByRole('main');
    expect(
      await within(main).findByRole('heading', { name: /no wallets yet/i }),
    ).toBeInTheDocument();
    // An empty portfolio is not a total of zero.
    expect(screen.queryByRole('region', { name: 'Total value' })).not.toBeInTheDocument();
    expect(main.querySelector('data')).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    await user.click(within(main).getByRole('link', { name: /add a wallet/i }));

    expect(await screen.findByRole('form', { name: 'Add a wallet' })).toBeInTheDocument();
    expect(currentPath()).toBe('/wallets');
  });

  it('a failed balances read is the whole-page failure, with a retry', async () => {
    let failing = true;
    const { user } = openDashboard(healthyPortfolio(), [
      http.get(BALANCES_CURRENT_PATH, () =>
        failing ? problem(503, 'Service Unavailable', 'The database is not reachable.') : undefined,
      ),
    ]);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('heading')).toHaveTextContent(/could not load your portfolio/i);
    expect(alert).toHaveTextContent('The database is not reachable.');
    // A failure is not an empty portfolio and not a zero.
    expect(screen.queryByText(/no wallets yet/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Total value' })).not.toBeInTheDocument();

    failing = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(dataValues(await totalRegion())).toContain(HEALTHY.total);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a balances read that never reached the server says so in words', async () => {
    openDashboard(healthyPortfolio(), [
      http.get(BALANCES_CURRENT_PATH, () => HttpResponse.error()),
    ]);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/could not be reached/i);
    expect(alert).not.toHaveTextContent(/failed to fetch/i);
  });

  it('a sync in progress is announced, and the rows are judged by the last finished run', async () => {
    const scenario = healthyPortfolio();
    openDashboard({ ...scenario, runs: [runningRun(), syncRun()] });
    await loaded();

    expect(await screen.findByText(/a sync is running/i)).toHaveAttribute('role', 'status');
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();
    // Judged against the run that finished, not the one with no outcomes yet.
    expect(cell(await walletRow('Cold storage'), 'Freshness')).toHaveTextContent(/up to date/i);
    expect(await lastUpdated()).toHaveTextContent(/last sync succeeded 15 minutes ago/i);
  });

  it('a first sync still running says so, and that none has finished', async () => {
    const wallets = [wallet({ id: 1, address: ADDRESSES.btcSegwit, label: 'First wallet' })];
    openDashboard({ wallets, runs: [runningRun()] });
    await loaded();

    expect(await screen.findByText(/a sync is running/i)).toBeInTheDocument();
    expect(await lastUpdated()).toHaveTextContent(NEVER_SYNCED_MESSAGE);
    expectNoRenderedZero(await walletRow('First wallet'));
  });

  it('the dashboard asks for exactly two runs', async () => {
    const { fake } = openDashboard();
    await loaded();

    const runReads = fake.requests.filter(
      (entry) => new URL(entry.url).pathname === BALANCES_RUNS_PATH,
    );
    expect(runReads.length).toBeGreaterThan(0);
    for (const read of runReads) {
      expect(new URL(read.url).searchParams.get('limit')).toBe('2');
    }
  });

  it('a 401 on the balances read returns to the login page', async () => {
    openDashboard(healthyPortfolio(), [
      http.get(BALANCES_CURRENT_PATH, () =>
        problem(401, 'Unauthorized', 'Authentication is required.'),
      ),
    ]);

    await waitFor(() => {
      expect(currentPath()).toBe('/login');
    });
    await settle();
    expect(currentPath()).toBe('/login');
  });
});
