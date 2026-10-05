import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import { PORTFOLIO_SUMMARY_PATH, type PortfolioSummary } from '@/api/portfolio';
import { fakePortfolio, type FakePortfolio, type FakePortfolioOptions } from '@/test/fakePortfolio';
import { healthyPortfolio, syncRun, triggered } from '@/test/fixtures';
import { currentPath, renderApp, settle, type ProvidedRender } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import {
  BTC_HOLDING,
  GAINING_SUMMARY,
  KAS_HOLDING,
  LOSING_SUMMARY,
  missing,
  portfolioSummary,
  unpricedHolding,
} from '@/test/summaryFixtures';

const FIGURES = ['Total value', 'Invested', 'Profit / loss'] as const;
type Figure = (typeof FIGURES)[number];

interface Opened extends ProvidedRender {
  readonly user: ReturnType<typeof userEvent.setup>;
  readonly fake: FakePortfolio;
}

/**
 * Opens the dashboard over the healthy portfolio, serving `summary` (or, when it is undefined,
 * the summary the fake derives from its wallets). `overrides` answer before the fake does.
 */
function openDashboard(
  summary: PortfolioSummary | undefined,
  options: FakePortfolioOptions = {},
  overrides: readonly HttpHandler[] = [],
): Opened {
  const user = userEvent.setup();
  const fake = fakePortfolio({
    ...healthyPortfolio(),
    ...(summary === undefined ? {} : { summary }),
    ...options,
  });
  server.use(
    ...overrides,
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fake.handlers,
  );
  return Object.assign(renderApp(['/']), { user, fake });
}

function figure(name: Figure): HTMLElement {
  return screen.getByRole('region', { name });
}

async function loaded(): Promise<void> {
  await screen.findByRole('region', { name: 'Total value' });
}

/** The figure as read: its parts - amount, unit, return - one space apart, or its dash. */
function figureText(name: Figure): string {
  const value = figure(name).querySelector('.kpi-value');
  if (value === null) {
    throw new Error(`No value in ${name}.`);
  }
  const parts = [...value.children].map((part) => part.textContent);
  return parts.length === 0 ? value.textContent : parts.join(' ');
}

function partial(name: Figure): boolean {
  return within(figure(name)).queryByText('Partial') !== null;
}

function holdingRow(asset: string): HTMLElement {
  const table = screen.getByRole('region', { name: 'Holdings table' });
  const header = within(table).getByRole('rowheader', { name: asset });
  const row = header.closest('tr');
  if (row === null) {
    throw new Error(`No row for ${asset}.`);
  }
  return row;
}

function cells(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent);
}

function summaryRequests(fake: FakePortfolio): number {
  return fake.requests.filter((entry) => new URL(entry.url).pathname === PORTFOLIO_SUMMARY_PATH)
    .length;
}

describe('DashboardPage: the figures', () => {
  it('shows what is held, what went in and the gain, in USDT', async () => {
    openDashboard(GAINING_SUMMARY);
    await loaded();

    expect(figureText('Total value')).toBe('30,770.00 USDT');
    expect(figureText('Invested')).toBe('30,701.30 USDT');
    // The direction is in the sign and the arrow, not only in a colour.
    expect(figureText('Profit / loss')).toBe('+68.70 USDT ▲+0.22 %');
    expect(figure('Profit / loss')).toHaveClass('kpi-gain');
    for (const name of FIGURES) {
      expect(partial(name)).toBe(false);
    }
    // Every figure carries the exact string it was formatted from.
    expect(within(figure('Total value')).getByText('30,770.00')).toHaveAttribute(
      'value',
      GAINING_SUMMARY.total_value,
    );
  });

  it('shows a loss with a minus sign and a down arrow', async () => {
    openDashboard(LOSING_SUMMARY);
    await loaded();

    expect(figureText('Profit / loss')).toBe('-4,230.00 USDT ▼-12.09 %');
    expect(figure('Profit / loss')).toHaveClass('kpi-loss');
  });

  it('shows a P/L of exactly zero with no sign, no arrow and no tone', async () => {
    openDashboard(
      portfolioSummary({
        total_value: '800.000000000000000000',
        invested: '800.000000000000000000',
        pnl: '0.000000000000000000',
        pnl_pct: '0.0000',
        holdings: [{ ...KAS_HOLDING, share_pct: '100.0000' }],
      }),
    );
    await loaded();

    expect(figureText('Profit / loss')).toBe('0.00 USDT 0.00 %');
    expect(figure('Profit / loss')).not.toHaveClass('kpi-gain');
    expect(figure('Profit / loss')).not.toHaveClass('kpi-loss');
  });

  it('leaves the percentage out when nothing was invested', async () => {
    // Coins that arrived with no trade: a value, nothing in, and no return to divide by.
    openDashboard(
      portfolioSummary({
        total_value: '800.000000000000000000',
        pnl: '800.000000000000000000',
        holdings: [{ ...KAS_HOLDING, share_pct: '100.0000' }],
      }),
    );
    await loaded();

    expect(figureText('Invested')).toBe('0.00 USDT');
    expect(figureText('Profit / loss')).toBe('+800.00 USDT');
  });
});

describe('DashboardPage: what a figure is missing', () => {
  it('marks the value and the P/L partial, and says why in one line', async () => {
    const { user } = openDashboard(
      portfolioSummary({
        total_value: '29970.000000000000000000',
        invested: '30701.300000000000000000',
        pnl: '-731.300000000000000000',
        pnl_pct: '-2.3820',
        holdings: [{ ...BTC_HOLDING, share_pct: '100.0000' }, unpricedHolding('KAS', '8000')],
        missing: [missing('exchange_unread', 'bingx'), missing('unpriced', 'KAS')],
      }),
    );
    await loaded();

    expect(partial('Total value')).toBe(true);
    expect(partial('Invested')).toBe(false);
    expect(partial('Profit / loss')).toBe(true);
    // The chip sits beside the figure's name, not in it.
    expect(figure('Total value')).toHaveAccessibleName('Total value');

    const note = screen.getByText(/^Incomplete:/).closest('p');
    expect(note).toHaveTextContent(
      'Incomplete: BingX balances not read yet; no price for KAS. See details',
    );

    // The unpriced holding is a row with a dash where a figure would be, not a zero.
    expect(cells(holdingRow('KAS'))).toEqual(['8,000', '—', '—', '—']);

    await user.click(within(note as HTMLElement).getByRole('link', { name: 'See details' }));
    expect(currentPath()).toBe('/details');
  });

  it('marks the invested figure and the P/L partial when a trade was not paid in cash', async () => {
    openDashboard({ ...GAINING_SUMMARY, missing: [missing('fill_not_in_cash', 'BTC')] });
    await loaded();

    expect(partial('Total value')).toBe(false);
    expect(partial('Invested')).toBe(true);
    expect(partial('Profit / loss')).toBe(true);
    expect(screen.getByText(/^Incomplete:/).closest('p')).toHaveTextContent(
      'trades paid in BTC not counted',
    );
  });

  it('shows a dash, not a zero, when nothing held could be valued', async () => {
    openDashboard(
      portfolioSummary({
        invested: '30701.300000000000000000',
        pnl: '-30701.300000000000000000',
        holdings: [unpricedHolding('BTC', '0.4995'), unpricedHolding('KAS', '8000')],
        missing: [missing('unpriced', 'BTC'), missing('unpriced', 'KAS')],
      }),
    );
    await loaded();

    expect(figureText('Total value')).toBe('—');
    expect(figureText('Profit / loss')).toBe('—');
    expect(figureText('Invested')).toBe('30,701.30 USDT');
    // No slice to draw, so no chart; the table still lists both.
    expect(screen.queryByRole('figure')).not.toBeInTheDocument();
    expect(holdingRow('BTC')).toBeInTheDocument();
    expect(holdingRow('KAS')).toBeInTheDocument();
  });

  it('names the assets nothing tracks, and does not call the total partial for them', async () => {
    openDashboard({ ...GAINING_SUMMARY, untracked: ['ZZDUST'] });
    await loaded();

    expect(screen.getByText('Not tracked, so not counted: ZZDUST.')).toBeInTheDocument();
    expect(partial('Total value')).toBe(false);
    expect(screen.queryByText(/^Incomplete:/)).not.toBeInTheDocument();
  });

  it('shows the footnote alone when untracked dust is all there is', async () => {
    openDashboard(portfolioSummary({ untracked: ['ZZDUST'] }));
    await loaded();

    const holdings = screen.getByRole('region', { name: 'Holdings' });
    expect(within(holdings).queryByRole('table')).not.toBeInTheDocument();
    expect(within(holdings).getByText('Not tracked, so not counted: ZZDUST.')).toBeInTheDocument();
  });
});

describe('DashboardPage: the holdings', () => {
  it('lists each holding with its amount, price, value and share', async () => {
    openDashboard(GAINING_SUMMARY);
    await loaded();

    expect(cells(holdingRow('BTC'))).toEqual(['0.4995', '60,000.00', '29,970.00', '97.40 %']);
    expect(cells(holdingRow('KAS'))).toEqual(['8,000', '0.10', '800.00', '2.60 %']);
    // Largest first, as the backend ordered them.
    const table = screen.getByRole('region', { name: 'Holdings table' });
    expect(
      within(table)
        .getAllByRole('rowheader')
        .map((header) => header.textContent),
    ).toEqual(['BTC', 'KAS']);
  });

  it('carries every share of the donut as text in its legend', async () => {
    openDashboard(GAINING_SUMMARY);
    await loaded();

    const donut = screen.getByRole('figure', { name: 'Allocation by value' });
    expect(
      within(donut)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['BTC97.40 %', 'KAS2.60 %']);
  });

  it('colours an asset the same in the legend and the table', async () => {
    openDashboard(GAINING_SUMMARY);
    await loaded();

    const donut = screen.getByRole('figure', { name: 'Allocation by value' });
    const legendSwatch = within(donut).getByText('BTC').previousElementSibling;
    const rowSwatch = holdingRow('BTC').querySelector('.swatch');
    expect(legendSwatch).toHaveStyle({ background: 'var(--series-orange)' });
    expect(rowSwatch).toHaveStyle({ background: 'var(--series-orange)' });
  });

  it('gives no slice to a holding whose share rounds to nothing', async () => {
    openDashboard({
      ...GAINING_SUMMARY,
      holdings: [
        { ...BTC_HOLDING, share_pct: '100.0000' },
        { ...KAS_HOLDING, value: '0.000000000000000000', share_pct: '0.0000' },
      ],
    });
    await loaded();

    const donut = screen.getByRole('figure', { name: 'Allocation by value' });
    expect(within(donut).getAllByRole('listitem')).toHaveLength(1);
    expect(holdingRow('KAS')).toBeInTheDocument();
  });
});

describe('DashboardPage: states', () => {
  it('says it is loading until the summary arrives', async () => {
    let release: () => void = () => undefined;
    const arrived = new Promise<void>((resolve) => {
      release = resolve;
    });
    openDashboard(GAINING_SUMMARY, {}, [
      http.get(PORTFOLIO_SUMMARY_PATH, async () => {
        await arrived;
        return undefined;
      }),
    ]);

    // Found by its words: the session check before it is a `status` of its own.
    expect(await screen.findByText('Loading your portfolio…')).toHaveAttribute('role', 'status');

    release();
    await loaded();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('says the summary could not be read, and reads it again on request', async () => {
    let fail = true;
    const { user } = openDashboard(GAINING_SUMMARY, {}, [
      http.get(PORTFOLIO_SUMMARY_PATH, () =>
        fail ? problem(500, 'Internal Server Error', 'The price table is locked.') : undefined,
      ),
    ]);

    const alert = await screen.findByRole('alert');
    expect(
      within(alert).getByRole('heading', { name: 'Could not load your portfolio' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The price table is locked.');
    expect(screen.queryByRole('region', { name: 'Total value' })).not.toBeInTheDocument();

    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));
    await loaded();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the figures on screen when a later read fails', async () => {
    const { queryClient } = openDashboard(GAINING_SUMMARY);
    await loaded();

    server.use(
      http.get(PORTFOLIO_SUMMARY_PATH, () =>
        problem(503, 'Service Unavailable', 'The backend is restarting.'),
      ),
    );
    await queryClient.refetchQueries({ queryKey: ['portfolio'] });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Could not refresh: The backend is restarting. Showing what was last loaded.',
    );
    expect(figureText('Total value')).toBe('30,770.00 USDT');
  });

  it('asks for a wallet or an exchange when there is nothing at all', async () => {
    openDashboard(undefined, { wallets: [] });

    expect(await screen.findByRole('heading', { name: 'Nothing to show yet' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add a wallet' })).toHaveAttribute('href', '/wallets');
    expect(screen.getByRole('link', { name: 'Connect an exchange' })).toHaveAttribute(
      'href',
      '/exchanges',
    );
    expect(screen.queryByRole('region', { name: 'Total value' })).not.toBeInTheDocument();
  });

  it('shows the figures, not the empty state, for wallets no run has read yet', async () => {
    // Nothing held and nothing invested, but there is something to wait for: an unread
    // wallet is a gap in the figures, not an empty portfolio.
    openDashboard(undefined);
    await loaded();

    expect(screen.queryByRole('heading', { name: 'Nothing to show yet' })).not.toBeInTheDocument();
    expect(partial('Total value')).toBe(true);
    expect(screen.getByText(/^Incomplete:/).closest('p')).toHaveTextContent(
      'Bitcoin wallet not read yet; Kaspa wallet not read yet.',
    );
  });

  it('shows the figures when everything was sold: something went in', async () => {
    openDashboard(
      portfolioSummary({
        invested: '-120.000000000000000000',
        pnl: '120.000000000000000000',
      }),
    );
    await loaded();

    expect(figureText('Invested')).toBe('-120.00 USDT');
    expect(figureText('Profit / loss')).toBe('+120.00 USDT');
    expect(screen.queryByRole('region', { name: 'Holdings' })).not.toBeInTheDocument();
  });
});

describe('DashboardPage: refresh', () => {
  it('reads the balances, then reads the summary again', async () => {
    const { user, fake } = openDashboard(undefined, {
      onSync: (portfolio) => {
        portfolio.setSummary(GAINING_SUMMARY);
        return triggered(syncRun({ trigger: 'manual' }));
      },
    });
    await loaded();
    const before = summaryRequests(fake);

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    await waitFor(() => {
      expect(figureText('Total value')).toBe('30,770.00 USDT');
    });
    expect(fake.writes('POST', '/api/balances/sync')).toHaveLength(1);
    expect(summaryRequests(fake)).toBeGreaterThan(before);
  });

  it('says a refresh is under way, and disables the button meanwhile', async () => {
    const { user, fake } = openDashboard(GAINING_SUMMARY);
    await loaded();
    const release = fake.hold('sync');

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Reading balances… this can take a minute.',
    );
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled();

    release();
    await waitFor(() => {
      expect(screen.queryByRole('status')).not.toBeInTheDocument();
    });
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled();
  });

  it('says a refresh did not complete, without claiming the sync stopped', async () => {
    const { user } = openDashboard(GAINING_SUMMARY, {}, [
      http.post('/api/balances/sync', () =>
        problem(409, 'Conflict', 'A balance sync is already running.'),
      ),
    ]);
    await loaded();

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Refresh did not complete: A balance sync is already running. A sync may still be running on the server; this page updates when it finishes.',
    );
    await settle();
    expect(figureText('Total value')).toBe('30,770.00 USDT');
  });
});
