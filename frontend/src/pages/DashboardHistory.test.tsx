import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import { PORTFOLIO_HISTORY_PATH, type HistoryRange } from '@/api/history';
import { GAPS_NOTE, PORTFOLIO_EMPTY_WORDS } from '@/lib/history';
import { fakePortfolio, type FakePortfolio, type FakePortfolioOptions } from '@/test/fakePortfolio';
import { healthyPortfolio, syncRun, triggered } from '@/test/fixtures';
import {
  GAPPY_HISTORY,
  historySettled,
  portfolioHistory,
  RANGE_DAYS,
  VALUE_A,
  VALUE_TODAY,
  WHOLE_HISTORY,
} from '@/test/historyFixtures';
import { renderApp, type ProvidedRender } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { VALUED_SUMMARY } from '@/test/summaryFixtures';

/**
 * The dashboard's chart of the value over time (spec 037): its four states, its range, and
 * that a day nothing could value is drawn as a gap and never as zero.
 */

const CARD = 'Value over time';
const LOADING = 'Loading the value history…';

interface Opened extends ProvidedRender {
  readonly user: ReturnType<typeof userEvent.setup>;
  readonly fake: FakePortfolio;
}

/** The dashboard over the valued summary. `overrides` answer before the fake does. */
function openDashboard(
  options: FakePortfolioOptions = {},
  overrides: readonly HttpHandler[] = [],
): Opened {
  const user = userEvent.setup();
  const fake = fakePortfolio({ ...healthyPortfolio(), summary: VALUED_SUMMARY, ...options });
  server.use(
    ...overrides,
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fake.handlers,
  );
  return Object.assign(renderApp(['/']), { user, fake });
}

/** Holds every history request until the returned function is called, then lets the fake answer. */
function holdHistory(): { readonly handler: HttpHandler; readonly release: () => void } {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const handler = http.get(PORTFOLIO_HISTORY_PATH, async () => {
    await gate;
    return undefined;
  });
  return { handler, release };
}

/**
 * Every range the dashboard asked the history for, in the order first asked. Each only once:
 * `StrictMode` mounts twice, and a first read it cancels is read again.
 */
function rangesRequested(fake: FakePortfolio): (string | null)[] {
  const ranges = fake.requests
    .map((entry) => new URL(entry.url))
    .filter((url) => url.pathname === PORTFOLIO_HISTORY_PATH)
    .map((url) => url.searchParams.get('range'));
  return [...new Set(ranges)];
}

function historyReads(fake: FakePortfolio): number {
  return fake.requests.filter((entry) => new URL(entry.url).pathname === PORTFOLIO_HISTORY_PATH)
    .length;
}

/** The figure by its accessible name: what is drawn, over which range. */
function chart(card: HTMLElement, range: string): HTMLElement {
  return within(card).getByRole('figure', { name: `Portfolio value, ${range}` });
}

function pressed(card: HTMLElement): string[] {
  return within(within(card).getByRole('group', { name: 'Range' }))
    .getAllByRole('button', { pressed: true })
    .map((button) => button.textContent);
}

/** A history of `range`'s length, every day valued, so each range draws a different chart. */
function valuedFor(range: HistoryRange) {
  return portfolioHistory(Array<string>(RANGE_DAYS[range]).fill(VALUE_A), range);
}

describe('DashboardPage: the value over time', () => {
  it('sits between the total and the holdings, and starts on 90 days', async () => {
    const { fake } = openDashboard({ history: GAPPY_HISTORY });

    const card = await historySettled(CARD);

    const regions = screen
      .getAllByRole('region')
      .map((region) => region.getAttribute('aria-labelledby'));
    expect(regions.indexOf('portfolio-history-heading')).toBeGreaterThan(
      regions.indexOf('kpi-value'),
    );
    expect(regions.indexOf('portfolio-history-heading')).toBeLessThan(
      regions.indexOf('holdings-heading'),
    );
    expect(pressed(card)).toEqual(['90D']);
    expect(within(card).getByText('USDT')).toBeInTheDocument();
    expect(rangesRequested(fake)).toEqual(['90d']);
  });

  it('draws the days it could value, and says what the gaps between them are', async () => {
    openDashboard({ history: GAPPY_HISTORY });

    const card = await historySettled(CARD);

    const figure = chart(card, 'the last 90 days');
    // Two runs of valued days: drawn at zero, the gaps would have joined them into one line.
    const line = figure.querySelector('.recharts-area-curve')?.getAttribute('d') ?? '';
    expect(line.split('M')).toHaveLength(3);
    expect(figure).toHaveTextContent(
      'From 29,000.00 USDT on Sep 21, 2026 to 30,770.00 USDT on Sep 24, 2026.',
    );
    expect(within(card).getByText(GAPS_NOTE)).toBeInTheDocument();
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says when the line will start while no day could be valued, without a zero', async () => {
    // The fake's default: every day of the range null, as before any wallet is read.
    openDashboard();

    const card = await historySettled(CARD);

    expect(within(card).getByText(PORTFOLIO_EMPTY_WORDS)).toBeInTheDocument();
    expect(within(card).queryByRole('figure')).not.toBeInTheDocument();
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(card).queryByText(GAPS_NOTE)).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent(/\b0\.00\b/);
  });

  it('announces that it is loading, inside its own card, with the figures already shown', async () => {
    const held = holdHistory();
    openDashboard({ history: WHOLE_HISTORY }, [held.handler]);

    const card = await screen.findByRole('region', { name: CARD });
    expect(within(card).getByRole('status')).toHaveTextContent(LOADING);
    expect(screen.getByRole('region', { name: 'Total value' })).toHaveTextContent('30,770.00');

    held.release();

    expect(await within(card).findByRole('figure')).toBeInTheDocument();
    expect(within(card).queryByRole('status')).not.toBeInTheDocument();
  });

  it('says the history could not be read, keeps the figures, and reads it again on request', async () => {
    let failing = true;
    const { user } = openDashboard({ history: WHOLE_HISTORY }, [
      http.get(PORTFOLIO_HISTORY_PATH, () =>
        failing ? problem(500, 'Internal Server Error', 'The price history is locked.') : undefined,
      ),
    ]);

    const card = await historySettled(CARD);
    const alert = within(card).getByRole('alert');
    expect(
      within(alert).getByRole('heading', { name: 'Could not load the value history' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The price history is locked.');
    // A failure is not an empty history, and the figures above it stand.
    expect(within(card).queryByText(PORTFOLIO_EMPTY_WORDS)).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Total value' })).toHaveTextContent('30,770.00');

    failing = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await within(card).findByRole('figure')).toBeInTheDocument();
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says so in words when the server could not be reached at all', async () => {
    openDashboard({}, [http.get(PORTFOLIO_HISTORY_PATH, () => HttpResponse.error())]);

    const card = await historySettled(CARD);

    expect(within(card).getByRole('alert')).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('keeps the chart on screen when a later read fails', async () => {
    const { queryClient } = openDashboard({ history: WHOLE_HISTORY });
    const card = await historySettled(CARD);

    server.use(
      http.get(PORTFOLIO_HISTORY_PATH, () =>
        problem(503, 'Service Unavailable', 'The backend is restarting.'),
      ),
    );
    await queryClient.refetchQueries({ queryKey: ['portfolio', 'history'] });

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'Could not refresh the value history: The backend is restarting. Showing what was last loaded.',
    );
    expect(chart(card, 'the last 90 days')).toHaveTextContent('30,770.00 USDT on Sep 24, 2026.');
  });

  it('reads the range chosen, keeping the previous one on screen until it arrives', async () => {
    const { user, fake } = openDashboard({ history: valuedFor });
    const card = await historySettled(CARD);
    expect(chart(card, 'the last 90 days')).toHaveAttribute('aria-busy', 'false');

    const held = holdHistory();
    server.use(held.handler);
    await user.click(within(card).getByRole('button', { name: '30D' }));

    expect(pressed(card)).toEqual(['30D']);
    // Still the 90 days, named as such, and marked as being replaced.
    await waitFor(() => {
      expect(chart(card, 'the last 90 days')).toHaveAttribute('aria-busy', 'true');
    });
    expect(within(card).queryByRole('status')).not.toBeInTheDocument();

    held.release();

    await waitFor(() => {
      expect(chart(card, 'the last 30 days')).toHaveAttribute('aria-busy', 'false');
    });

    await user.click(within(card).getByRole('button', { name: '1Y' }));
    expect(
      await within(card).findByRole('figure', { name: 'Portfolio value, the last year' }),
    ).toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'All' }));
    expect(
      await within(card).findByRole('figure', { name: 'Portfolio value, since the first reading' }),
    ).toBeInTheDocument();
    expect(pressed(card)).toEqual(['All']);

    expect(rangesRequested(fake)).toEqual(['90d', '30d', '1y', 'all']);
  });

  it('is read again after a refresh, with the balances it values', async () => {
    const { user, fake } = openDashboard({
      onSync: (portfolio) => {
        portfolio.setHistory(portfolioHistory([null, VALUE_TODAY]));
        return triggered(syncRun({ trigger: 'manual' }));
      },
    });
    const card = await historySettled(CARD);
    expect(within(card).getByText(PORTFOLIO_EMPTY_WORDS)).toBeInTheDocument();
    const before = historyReads(fake);

    await user.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await within(card).findByRole('figure')).toHaveTextContent(
      'Valued on one day only: 30,770.00 USDT on Sep 24, 2026.',
    );
    expect(historyReads(fake)).toBeGreaterThan(before);
  });
});
