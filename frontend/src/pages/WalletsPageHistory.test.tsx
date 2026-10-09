import { render, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import { WALLET_EMPTY_WORDS } from '@/lib/history';
import { WalletHistory } from '@/pages/dashboard/WalletHistory';
import {
  fakePortfolio,
  WALLET_NOT_FOUND_DETAIL,
  WALLET_VALUE_HISTORY_PATH,
  type FakePortfolio,
  type FakePortfolioOptions,
  type WalletHistoryView,
} from '@/test/fakePortfolio';
import { currentBalances, healthyPortfolio, walletBalance } from '@/test/fixtures';
import { historySettled, RANGE_DAYS, walletValueHistory } from '@/test/historyFixtures';
import { renderApp, type ProvidedRender } from '@/test/render';
import { fakeSession, server, TEST_USERNAME } from '@/test/server';

/**
 * The Wallets page's chart of one wallet's value over time (spec 037): which wallet, which
 * range, and its four states. It was the Details page's until spec 039 folded that page in.
 */

const CARD = 'Wallet value over time';
const COLD_VALUE = '78000.000000000000000000';

interface Opened extends ProvidedRender {
  readonly user: ReturnType<typeof userEvent.setup>;
  readonly fake: FakePortfolio;
}

function openWalletsPage(
  options: FakePortfolioOptions = {},
  overrides: readonly HttpHandler[] = [],
): Opened {
  const user = userEvent.setup();
  const fake = fakePortfolio({ ...healthyPortfolio(), ...options });
  server.use(
    ...overrides,
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fake.handlers,
  );
  return Object.assign(renderApp(['/wallets']), { user, fake });
}

/**
 * Every wallet worth something on each day of the range: a quantity, then that quantity's
 * value. Cold storage was read two days ago, so its first day is a gap.
 */
const valued: WalletHistoryView = (wallet, range) => {
  const days = RANGE_DAYS[range];
  const read = Array.from({ length: days - 1 }, () => ['1.50000000', COLD_VALUE] as const);
  return walletValueHistory(
    wallet.id,
    wallet.chain_key === 'kaspa' ? 'KAS' : 'BTC',
    [[null, null], ...read],
    range,
  );
};

/** Holds every wallet-history request until released, then lets the fake answer. */
function holdWalletHistory(): { readonly handler: HttpHandler; readonly release: () => void } {
  let release: () => void = () => undefined;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const handler = http.get(WALLET_VALUE_HISTORY_PATH, async () => {
    await gate;
    return undefined;
  });
  return { handler, release };
}

/** Each wallet and range asked for, in the order first asked. */
function requested(fake: FakePortfolio): string[] {
  const paths = fake.requests
    .map((entry) => new URL(entry.url))
    .filter((url) => /^\/api\/wallets\/\d+\/value-history$/.test(url.pathname))
    .map((url) => `${url.pathname}${url.search}`);
  return [...new Set(paths)];
}

function walletSelect(card: HTMLElement): HTMLSelectElement {
  return within(card).getByRole('combobox', { name: 'Wallet' });
}

describe('WalletsPage: one wallet over time', () => {
  it('offers every wallet the tables list, named as they name it, and starts on the first', async () => {
    const { fake } = openWalletsPage();

    const card = await historySettled(CARD);

    const select = walletSelect(card);
    expect(
      within(select)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Cold storage (BTC)', 'Spending (BTC)', 'Kaspa wallet #3 (KAS)']);
    expect(select).toHaveDisplayValue('Cold storage (BTC)');
    expect(requested(fake)).toEqual(['/api/wallets/1/value-history?range=90d']);
  });

  it('says when the line will start for a wallet not read yet, without a zero', async () => {
    openWalletsPage();

    const card = await historySettled(CARD);

    expect(within(card).getByText(WALLET_EMPTY_WORDS)).toBeInTheDocument();
    expect(within(card).queryByRole('figure')).not.toBeInTheDocument();
    expect(within(card).queryByRole('alert')).not.toBeInTheDocument();
  });

  it("draws the wallet's value in its asset's colour", async () => {
    openWalletsPage({ walletHistory: valued });

    const card = await historySettled(CARD);

    const figure = within(card).getByRole('figure', {
      name: 'Cold storage value, the last 90 days',
    });
    expect(figure.querySelector('.recharts-area-curve')).toHaveAttribute(
      'stroke',
      'var(--series-orange)',
    );
    expect(figure).toHaveTextContent(
      'From 78,000.00 USDT on Jun 28, 2026 to 78,000.00 USDT on Sep 24, 2026.',
    );
  });

  it('reads another wallet when chosen, never showing the last one under its name', async () => {
    const { user, fake } = openWalletsPage({ walletHistory: valued });
    const card = await historySettled(CARD);

    const held = holdWalletHistory();
    server.use(held.handler);
    await user.selectOptions(walletSelect(card), 'Kaspa wallet #3 (KAS)');

    // The Cold storage line is gone at once: another wallet's line would be a wrong answer.
    expect(await within(card).findByRole('status')).toHaveTextContent('Loading the value history…');
    expect(within(card).queryByRole('figure')).not.toBeInTheDocument();

    held.release();

    const figure = await within(card).findByRole('figure', {
      name: 'Kaspa wallet #3 value, the last 90 days',
    });
    expect(figure.querySelector('.recharts-area-curve')).toHaveAttribute(
      'stroke',
      'var(--series-aqua)',
    );
    expect(requested(fake)).toEqual([
      '/api/wallets/1/value-history?range=90d',
      '/api/wallets/3/value-history?range=90d',
    ]);
  });

  it("reads the range chosen, keeping the same wallet's line until it arrives", async () => {
    const { user, fake } = openWalletsPage({ walletHistory: valued });
    const card = await historySettled(CARD);

    const held = holdWalletHistory();
    server.use(held.handler);
    await user.click(within(card).getByRole('button', { name: '1Y' }));

    await waitFor(() => {
      expect(
        within(card).getByRole('figure', { name: 'Cold storage value, the last 90 days' }),
      ).toHaveAttribute('aria-busy', 'true');
    });

    held.release();

    expect(
      await within(card).findByRole('figure', { name: 'Cold storage value, the last year' }),
    ).toHaveAttribute('aria-busy', 'false');
    expect(requested(fake)).toEqual([
      '/api/wallets/1/value-history?range=90d',
      '/api/wallets/1/value-history?range=1y',
    ]);
  });

  it('says a wallet the server does not know could not be read', async () => {
    // A wallet in the balances that the registry no longer has: deleted between the two reads.
    const scenario = healthyPortfolio();
    openWalletsPage({
      current: currentBalances({
        ...scenario.current,
        wallets: [walletBalance({ wallet_id: 7, label: 'Gone' })],
      }),
    });

    const card = await historySettled(CARD);

    const alert = within(card).getByRole('alert');
    expect(alert).toHaveTextContent('Could not load the value history');
    expect(alert).toHaveTextContent(WALLET_NOT_FOUND_DETAIL);
  });

  it('goes back to the first wallet when the chosen one leaves the list', async () => {
    const { user, fake, queryClient } = openWalletsPage({ walletHistory: valued });
    const card = await historySettled(CARD);
    await user.selectOptions(walletSelect(card), 'Spending (BTC)');
    await within(card).findByRole('figure', { name: 'Spending value, the last 90 days' });

    const scenario = healthyPortfolio();
    fake.setCurrent({
      ...scenario.current,
      wallets: scenario.current.wallets.filter((wallet) => wallet.wallet_id !== 2),
    });
    await queryClient.refetchQueries({ queryKey: ['balances'] });

    await waitFor(() => {
      expect(walletSelect(card)).toHaveDisplayValue('Cold storage (BTC)');
    });
    expect(
      await within(card).findByRole('figure', { name: 'Cold storage value, the last 90 days' }),
    ).toBeInTheDocument();
  });
});

describe('WalletHistory', () => {
  it('draws nothing when there is no wallet to choose', () => {
    const { container } = render(<WalletHistory wallets={[]} />);

    expect(container).toBeEmptyDOMElement();
  });
});
