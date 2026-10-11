import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import { PORTFOLIO_CHANGES_PATH } from '@/api/changes';
import { CHANGES_LOADING_LABEL } from '@/pages/dashboard/ChangeSummary';
import {
  changeOf,
  MOVED_CHANGES,
  portfolioChanges,
  unavailableChange,
} from '@/test/changeFixtures';
import { fakePortfolio, type FakePortfolioOptions } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { renderApp, type ProvidedRender } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { VALUED_SUMMARY } from '@/test/summaryFixtures';

/**
 * The dashboard's change over 24 hours and 7 days (spec 041): its states, its signs and
 * colours, and that a change nothing could work out says why rather than showing zero.
 */

const CARD = 'Change';

interface Opened extends ProvidedRender {
  readonly user: ReturnType<typeof userEvent.setup>;
}

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
  return Object.assign(renderApp(['/']), { user });
}

async function settledCard(): Promise<HTMLElement> {
  const card = await screen.findByRole('region', { name: CARD });
  await waitFor(() => {
    expect(within(card).queryByRole('status')).not.toBeInTheDocument();
  });
  return card;
}

function tile(card: HTMLElement, name: string): HTMLElement {
  return within(card).getByRole('group', { name });
}

describe('DashboardPage: the change over 24 hours and 7 days', () => {
  it('sits between the total and the value over time', async () => {
    openDashboard({ changes: MOVED_CHANGES });

    await settledCard();

    const regions = screen
      .getAllByRole('region')
      .map((region) => region.getAttribute('aria-labelledby'));
    expect(regions.indexOf('changes-heading')).toBeGreaterThan(regions.indexOf('kpi-value'));
    expect(regions.indexOf('changes-heading')).toBeLessThan(
      regions.indexOf('portfolio-history-heading'),
    );
  });

  it('shows a rise with + in green and a fall with - in red, each with its percentage', async () => {
    openDashboard({ changes: MOVED_CHANGES });

    const card = await settledCard();

    const day = tile(card, 'Last 24 hours');
    expect(day).toHaveClass('change-up');
    expect(day).toHaveTextContent('+770.00 USDT');
    expect(day).toHaveTextContent('+2.57%');
    const week = tile(card, 'Last 7 days');
    expect(week).toHaveClass('change-down');
    expect(week).toHaveTextContent('-1,230.00 USDT');
    expect(week).toHaveTextContent('-3.84%');
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows no change as an unsigned zero, in ink, and says when there is no percentage', async () => {
    openDashboard({
      changes: portfolioChanges(
        changeOf('24h', '0.000000000000000000', '0.0000'),
        changeOf('7d', '30770.000000000000000000', null, '0.000000000000000000'),
      ),
    });

    const card = await settledCard();

    const day = tile(card, 'Last 24 hours');
    expect(day).toHaveClass('change-flat');
    expect(day).toHaveTextContent('0.00 USDT');
    expect(day).not.toHaveTextContent('▲');
    expect(tile(card, 'Last 7 days')).toHaveTextContent('No percentage: nothing was held then.');
  });

  it('says why a change is missing, and never shows it as zero', async () => {
    openDashboard({
      changes: portfolioChanges(
        unavailableChange('24h', 'no_price_then'),
        unavailableChange('7d', 'no_reading_then'),
      ),
    });

    const card = await settledCard();

    expect(tile(card, 'Last 24 hours')).toHaveTextContent(
      'Not available: no price was recorded 24 hours ago.',
    );
    expect(tile(card, 'Last 7 days')).toHaveTextContent(
      'Not available: no balance is known for 7 days ago.',
    );
    expect(card).not.toHaveTextContent(/\b0\.00\b/);
  });

  it('says the total now is incomplete when the backend cannot value it', async () => {
    // The fake's default: the value now unknown, as while a wallet is unread.
    openDashboard();

    const card = await settledCard();

    expect(within(card).getAllByText('Not available: the total now is incomplete.')).toHaveLength(
      2,
    );
  });

  it('reads a change with neither a figure nor a reason as one it cannot value now', async () => {
    // Outside the contract, but a blank tile would read as nothing having moved.
    openDashboard({
      changes: portfolioChanges(
        { ...unavailableChange('24h', 'no_price_then'), unavailable: null },
        changeOf('7d', '0.000000000000000000', '0.0000'),
      ),
    });

    const card = await settledCard();

    expect(tile(card, 'Last 24 hours')).toHaveTextContent(
      'Not available: the total now is incomplete.',
    );
  });

  it('announces that it is loading, inside its own card', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openDashboard({ changes: MOVED_CHANGES }, [
      http.get(PORTFOLIO_CHANGES_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);

    const card = await screen.findByRole('region', { name: CARD });
    expect(within(card).getByRole('status')).toHaveTextContent(CHANGES_LOADING_LABEL);

    release();

    expect(await within(card).findByText('+770.00 USDT')).toBeInTheDocument();
  });

  it('says the change could not be read, and reads it again on request', async () => {
    let failing = true;
    const { user } = openDashboard({ changes: MOVED_CHANGES }, [
      http.get(PORTFOLIO_CHANGES_PATH, () =>
        failing
          ? problem(500, 'Internal Server Error', 'The hourly prices are locked.')
          : undefined,
      ),
    ]);

    const card = await settledCard();
    const alert = within(card).getByRole('alert');
    expect(
      within(alert).getByRole('heading', { name: 'Could not load the change' }),
    ).toBeInTheDocument();
    expect(alert).toHaveTextContent('The hourly prices are locked.');
    expect(screen.getByRole('region', { name: 'Total value' })).toHaveTextContent('30,770.00');

    failing = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await within(card).findByText('+770.00 USDT')).toBeInTheDocument();
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says so in words when the server could not be reached at all', async () => {
    openDashboard({}, [http.get(PORTFOLIO_CHANGES_PATH, () => HttpResponse.error())]);

    const card = await settledCard();

    expect(within(card).getByRole('alert')).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('keeps the figures on screen when a later read fails', async () => {
    const { queryClient } = openDashboard({ changes: MOVED_CHANGES });
    const card = await settledCard();
    expect(within(card).getByText('+770.00 USDT')).toBeInTheDocument();

    server.use(
      http.get(PORTFOLIO_CHANGES_PATH, () =>
        problem(503, 'Service Unavailable', 'The backend is restarting.'),
      ),
    );
    await queryClient.refetchQueries({ queryKey: ['portfolio', 'changes'] });

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'Could not refresh the change: The backend is restarting. Showing what was last loaded.',
    );
    expect(within(card).getByText('+770.00 USDT')).toBeInTheDocument();
  });
});
