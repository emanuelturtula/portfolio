import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import { INVESTMENT_PATH } from '@/api/operations';
import { INVESTMENT_LOADING_LABEL } from '@/pages/dashboard/InvestmentSummary';
import { fakePortfolio, type FakePortfolioOptions } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { historySettled, WHOLE_HISTORY } from '@/test/historyFixtures';
import {
  assetInvestment,
  emptyInvestment,
  INVESTED,
  totalInvestment,
} from '@/test/investmentFixtures';
import { currentPath, renderApp, type ProvidedRender } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { VALUED_SUMMARY } from '@/test/summaryFixtures';

/**
 * The dashboard's Invested section (spec 042): its four states, a gain and a loss told apart
 * by sign and word as well as colour, a figure nothing could work out said in words and never
 * as zero, and the invested line beside the value over time.
 */

const CARD = 'Invested';

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

function totals(card: HTMLElement): string[] {
  return within(card)
    .getAllByRole('definition')
    .map((value) => value.textContent);
}

function row(card: HTMLElement, asset: string): string[] {
  const header = within(card).getByRole('rowheader', { name: asset });
  return within(header.closest('tr') as HTMLElement)
    .getAllByRole('cell')
    .map((cell) => cell.textContent);
}

describe('DashboardPage: what was invested', () => {
  it('announces the wait while the figures load', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openDashboard({ investment: INVESTED }, [
      http.get(INVESTMENT_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);

    const card = await screen.findByRole('region', { name: CARD });
    expect(within(card).getByRole('status')).toHaveTextContent(INVESTMENT_LOADING_LABEL);

    release();
    await settledCard();
    expect(totals(card)[0]).toBe('13,507.00 USDT');
  });

  it('says what was invested, what it is worth and the gain, with a sign and a word', async () => {
    openDashboard({ investment: INVESTED });

    const card = await settledCard();

    expect(totals(card)).toEqual([
      '13,507.00 USDT',
      '24,600.00 USDT',
      '+11,093.00 USDT gain (+82.13%)',
    ]);
    expect(within(card).getByText('+11,093.00 USDT gain (+82.13%)')).toHaveClass('change-up');
    expect(row(card, 'BTC')).toEqual([
      '13,007.00 USDT',
      '24,000.00 USDT',
      '+10,993.00 USDT gain (+84.52%)',
      '0.4 BTC',
      '0.4 BTC',
      'Matches the operations.',
    ]);
    expect(row(card, 'KAS')).toEqual([
      '700.00 USDT',
      '600.00 USDT',
      '-100.00 USDT loss (-14.29%)',
      '6,000 KAS',
      '6,100 KAS',
      '-100 KAS: the wallets hold less than the operations explain.',
    ]);
    expect(within(card).getByText('-100.00 USDT loss (-14.29%)')).toHaveClass('change-down');
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('says why a figure is missing, and never shows it as zero', async () => {
    openDashboard({
      investment: emptyInvestment({
        assets: [
          assetInvestment({
            invested: null,
            pnl: null,
            pnl_pct: null,
            unvalued_trades: 1,
            unavailable: 'unvalued_trades',
          }),
          assetInvestment({
            asset: 'KAS',
            value: null,
            pnl: null,
            pnl_pct: null,
            held: null,
            difference: null,
            unavailable: null,
          }),
          assetInvestment({
            asset: 'ETH',
            invested: '0',
            value: '50',
            pnl: '50',
            pnl_pct: null,
            unavailable: null,
          }),
        ],
        overall: totalInvestment({
          invested: null,
          pnl: null,
          pnl_pct: null,
          unavailable: 'unvalued_trades',
        }),
      }),
    });

    const card = await settledCard();

    expect(totals(card)).toEqual([
      '—',
      '24,600.00 USDT',
      'Not available: a trade was not priced in USDT, USDC or DAI.',
    ]);
    expect(row(card, 'BTC').slice(0, 3)).toEqual([
      '—',
      '24,000.00 USDT',
      'Not available: a trade was not priced in USDT, USDC or DAI.',
    ]);
    expect(row(card, 'KAS')).toEqual([
      '13,007.00 USDT',
      '—',
      'Not available: the value now is unknown.',
      'Not read yet',
      '0.4 KAS',
      'Unknown until the wallet is read.',
    ]);
    expect(row(card, 'ETH')[2]).toBe('+50.00 USDT gain (No percentage: nothing is invested.)');
  });

  it('points to the Operations page while nothing has been uploaded', async () => {
    const { user } = openDashboard({
      investment: emptyInvestment({ assets: [assetInvestment({ trades: 0 })] }),
    });

    const card = await settledCard();

    expect(card).toHaveTextContent(
      'No operations yet. Upload your exchange reports to see what was invested and the gain or loss.',
    );
    expect(within(card).queryByRole('table')).not.toBeInTheDocument();
    await user.click(within(card).getByRole('link', { name: 'Upload your exchange reports' }));
    await waitFor(() => {
      expect(currentPath()).toBe('/operations');
    });
  });

  it('says what failed and loads again on request', async () => {
    let fail = true;
    const { user } = openDashboard({ investment: INVESTED }, [
      http.get(INVESTMENT_PATH, () =>
        fail ? problem(500, 'Internal Server Error', 'The database is locked.') : undefined,
      ),
    ]);

    const card = await settledCard();
    const alert = within(card).getByRole('alert');
    expect(alert).toHaveTextContent('Could not load what was invested');
    expect(alert).toHaveTextContent('The database is locked.');

    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    await waitFor(() => {
      expect(totals(card)[0]).toBe('13,507.00 USDT');
    });
  });

  it('says so in words when the server could not be reached at all', async () => {
    openDashboard({}, [http.get(INVESTMENT_PATH, () => HttpResponse.error())]);

    const card = await settledCard();

    expect(within(card).getByRole('alert')).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('keeps the figures on screen when a later read fails', async () => {
    const { queryClient } = openDashboard({ investment: INVESTED });
    const card = await settledCard();

    server.use(
      http.get(INVESTMENT_PATH, () =>
        problem(503, 'Service Unavailable', 'The backend is restarting.'),
      ),
    );
    await queryClient.refetchQueries({ queryKey: ['portfolio', 'investment'] });

    expect(await within(card).findByRole('alert')).toHaveTextContent(
      'Could not refresh what was invested: The backend is restarting. Showing what was last loaded.',
    );
    expect(totals(card)[0]).toBe('13,507.00 USDT');
  });

  describe('beside the value over time', () => {
    function compare(card: HTMLElement): HTMLElement {
      return within(card).getByRole('group', { name: 'Compare' });
    }

    function figure(card: HTMLElement): HTMLElement {
      return within(card).getByRole('figure', { name: 'Portfolio value, the last 90 days' });
    }

    it('draws what was invested as a second line, and lets it go', async () => {
      const { user } = openDashboard({ investment: INVESTED, history: WHOLE_HISTORY });
      await settledCard();
      const card = await historySettled('Value over time');
      const button = await within(card).findByRole('button', { name: 'Invested' });

      expect(button).toHaveAttribute('aria-pressed', 'true');
      expect(figure(card).querySelectorAll('.recharts-area-curve')).toHaveLength(2);
      expect(figure(card)).toHaveTextContent(
        'Invested: From 13,507.00 USDT on Sep 22, 2026 to 13,507.00 USDT on Sep 24, 2026.',
      );

      await user.click(within(compare(card)).getByRole('button', { name: 'Invested' }));

      expect(button).toHaveAttribute('aria-pressed', 'false');
      expect(figure(card).querySelectorAll('.recharts-area-curve')).toHaveLength(1);
      expect(figure(card)).not.toHaveTextContent('Invested');
    });

    it('offers no invested line before anything is uploaded', async () => {
      openDashboard({ history: WHOLE_HISTORY });
      await settledCard();
      const card = await historySettled('Value over time');

      expect(within(card).queryByRole('group', { name: 'Compare' })).not.toBeInTheDocument();
      expect(figure(card).querySelectorAll('.recharts-area-curve')).toHaveLength(1);
    });
  });
});
