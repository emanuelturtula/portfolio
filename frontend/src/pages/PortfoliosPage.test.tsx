import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { IMPORTS_PATH, type ImportReport } from '@/api/operations';
import { PORTFOLIO_SUMMARY_PATH } from '@/api/portfolio';
import { PORTFOLIO_SCOPE_LOADING_LABEL, SELECTED_PORTFOLIO_KEY } from '@/lib/portfolioScope';
import { deleteBlocker, describeContents, PORTFOLIOS_LOADING_LABEL } from '@/pages/PortfoliosPage';
import { fakeOperations } from '@/test/fakeOperations';
import { fakePortfolio, WALLET_PATH, WALLETS_PATH } from '@/test/fakePortfolio';
import {
  fakePortfolios,
  NAME_TAKEN_DETAIL,
  portfolio,
  type FakePortfolios,
} from '@/test/fakePortfolios';
import { ADDRESSES, healthyPortfolio, wallet } from '@/test/fixtures';
import { operation } from '@/test/investmentFixtures';
import { renderApp } from '@/test/render';
import { fakeSession, PORTFOLIOS_PATH, problem, server, TEST_USERNAME } from '@/test/server';

/**
 * Spec 044 in the browser: the Portfolios page, the selector at the top of every page, the
 * portfolio field of the forms, and moving wallets and operations between portfolios.
 */

const MAIN = portfolio({ id: 1, name: 'Main', wallets: 2, operations: 3 });
const TRADING = portfolio({ id: 2, name: 'Trading' });
const RETIREMENT = portfolio({ id: 3, name: 'Retirement' });

function clearStored(): void {
  window.localStorage.removeItem(SELECTED_PORTFOLIO_KEY);
}

beforeEach(clearStored);
afterEach(clearStored);

function signIn(registry: FakePortfolios): void {
  server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers, ...registry.handlers);
}

function switcher(): HTMLSelectElement {
  return screen.getByRole('combobox', { name: 'Portfolio' });
}

describe('the words', () => {
  it('counts what a portfolio holds, one or many', () => {
    expect(describeContents(portfolio({ id: 1, name: 'A', wallets: 1, operations: 0 }))).toBe(
      '1 wallet, 0 operations',
    );
    expect(describeContents(portfolio({ id: 1, name: 'A', wallets: 2, operations: 1 }))).toBe(
      '2 wallets, 1 operation',
    );
  });

  it('says why a portfolio cannot be deleted, the last one first', () => {
    expect(deleteBlocker(MAIN, 1)).toBe('The only portfolio cannot be deleted.');
    expect(deleteBlocker(MAIN, 2)).toMatch(/Move its wallets and operations/);
    expect(deleteBlocker(TRADING, 2)).toBeUndefined();
  });
});

describe('the Portfolios page', () => {
  it('announces loading, then lists every portfolio with what it holds', async () => {
    const registry = fakePortfolios([MAIN, TRADING]);
    signIn(registry);
    renderApp(['/portfolios']);

    expect(await screen.findByText(PORTFOLIOS_LOADING_LABEL)).toBeInTheDocument();
    const list = await screen.findByRole('list');
    expect(within(list).getByText('2 wallets, 3 operations')).toBeInTheDocument();
    expect(within(list).getByText('0 wallets, 0 operations')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Main' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete Trading' })).toBeEnabled();
  });

  it('says so when the portfolios cannot be read, rather than showing none', async () => {
    signIn(fakePortfolios([]));
    server.use(
      http.get(PORTFOLIOS_PATH, () => problem(500, 'Internal Server Error', 'Database is locked.')),
    );
    renderApp(['/portfolios']);

    expect(
      await screen.findByRole('heading', { name: 'Could not load your portfolios' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('No portfolios yet')).not.toBeInTheDocument();
  });

  it('creates, renames and deletes a portfolio', async () => {
    const user = userEvent.setup();
    const registry = fakePortfolios([MAIN]);
    signIn(registry);
    renderApp(['/portfolios']);
    await screen.findByText('2 wallets, 3 operations');

    await user.type(screen.getByLabelText('Name'), '  Retirement ');
    await user.click(screen.getByRole('button', { name: 'Create' }));
    expect(await screen.findByText('Created Retirement.')).toBeInTheDocument();
    expect(registry.requests.find((request) => request.method === 'POST')?.body).toEqual({
      name: 'Retirement',
    });

    await user.click(await screen.findByRole('button', { name: 'Rename Retirement' }));
    const field = screen.getByLabelText('New name for Retirement');
    await user.clear(field);
    await user.type(field, 'Pension');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    await user.click(await screen.findByRole('button', { name: 'Delete Pension' }));

    await waitFor(() => {
      expect(registry.list().map((row) => row.name)).toEqual(['Main']);
    });
    await waitFor(() => {
      expect(screen.queryByText('Pension')).not.toBeInTheDocument();
    });
  });

  it('retries a failed read from the error state', async () => {
    const user = userEvent.setup();
    const registry = fakePortfolios([MAIN]);
    signIn(registry);
    server.use(
      http.get(
        PORTFOLIOS_PATH,
        () => problem(500, 'Internal Server Error', 'Database is locked.'),
        {
          once: true,
        },
      ),
    );
    renderApp(['/portfolios']);

    await user.click(await screen.findByRole('button', { name: /try again|retry/i }));

    expect(await screen.findByText('2 wallets, 3 operations')).toBeInTheDocument();
  });

  it('says there are none yet when the account has no portfolio', async () => {
    signIn(fakePortfolios([]));
    renderApp(['/portfolios']);

    expect(await screen.findByRole('heading', { name: 'No portfolios yet' })).toBeInTheDocument();
  });

  it('cancels a rename without sending it', async () => {
    const user = userEvent.setup();
    const registry = fakePortfolios([MAIN]);
    signIn(registry);
    renderApp(['/portfolios']);

    await user.click(await screen.findByRole('button', { name: 'Rename Main' }));
    await user.type(screen.getByLabelText('New name for Main'), ' fund');
    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Rename Main' }));

    expect(screen.getByLabelText('New name for Main')).toHaveValue('Main');
    expect(registry.requests.filter((request) => request.method === 'PATCH')).toEqual([]);
  });

  it('shows why a rename or a delete was refused', async () => {
    const user = userEvent.setup();
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(
      http.delete(`${PORTFOLIOS_PATH}/:id`, () =>
        problem(409, 'Conflict', 'This portfolio still holds wallets or operations.'),
      ),
    );
    renderApp(['/portfolios']);

    await user.click(await screen.findByRole('button', { name: 'Rename Trading' }));
    const field = screen.getByLabelText('New name for Trading');
    await user.clear(field);
    await user.type(field, 'Main');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(`Not renamed: ${NAME_TAKEN_DETAIL}`);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Delete Trading' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Not deleted: This portfolio still holds wallets or operations.',
    );
  });

  it('shows the refusal of a name already taken', async () => {
    const user = userEvent.setup();
    signIn(fakePortfolios([MAIN]));
    renderApp(['/portfolios']);
    await screen.findByText('2 wallets, 3 operations');

    await user.type(screen.getByLabelText('Name'), 'Main');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(NAME_TAKEN_DETAIL);
  });
});

describe('the selector', () => {
  it('is not shown with one portfolio, and every read is for all of them', async () => {
    const registry = fakePortfolios([MAIN]);
    const fake = fakePortfolio(healthyPortfolio());
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '1');
    signIn(registry);
    server.use(...fake.handlers);
    renderApp(['/']);

    await waitFor(() => {
      expect(fake.writes('GET', PORTFOLIO_SUMMARY_PATH).length).toBeGreaterThan(0);
    });
    expect(screen.queryByRole('combobox', { name: 'Portfolio' })).not.toBeInTheDocument();
    expect(
      fake.writes('GET', PORTFOLIO_SUMMARY_PATH).every((r) => !r.url.includes('portfolio=')),
    ).toBe(true);
  });

  it('narrows every read to the chosen portfolio and remembers the choice', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio(healthyPortfolio());
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/']);

    await waitFor(() => {
      expect(switcher()).toHaveValue('all');
    });
    expect(
      within(switcher())
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['All portfolios', 'Main', 'Trading']);

    await user.selectOptions(switcher(), 'Trading');

    await waitFor(() => {
      expect(
        fake.writes('GET', PORTFOLIO_SUMMARY_PATH).some((r) => r.url.endsWith('?portfolio=2')),
      ).toBe(true);
    });
    expect(window.localStorage.getItem(SELECTED_PORTFOLIO_KEY)).toBe('2');
  });

  it('goes back to all of them and forgets the choice', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio(healthyPortfolio());
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '2');
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/']);

    await waitFor(() => {
      expect(switcher()).toHaveValue('2');
    });
    await user.selectOptions(switcher(), 'All portfolios');

    expect(switcher()).toHaveValue('all');
    expect(window.localStorage.getItem(SELECTED_PORTFOLIO_KEY)).toBeNull();
  });

  it('still switches when the browser blocks storage, for as long as the page is open', async () => {
    const user = userEvent.setup();
    const blocked = () => {
      throw new DOMException('Blocked', 'SecurityError');
    };
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(blocked);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(blocked);
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(blocked);
    const fake = fakePortfolio(healthyPortfolio());
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/']);

    await waitFor(() => {
      expect(switcher()).toHaveValue('all');
    });
    await user.selectOptions(switcher(), 'Trading');
    expect(switcher()).toHaveValue('2');
    await user.selectOptions(switcher(), 'All portfolios');
    expect(switcher()).toHaveValue('all');
    vi.restoreAllMocks();
  });

  it('reads nothing for all of them while a remembered choice is being checked', async () => {
    const fake = fakePortfolio(healthyPortfolio());
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '2');
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/']);

    expect(await screen.findByText(PORTFOLIO_SCOPE_LOADING_LABEL)).toBeInTheDocument();
    await waitFor(() => {
      expect(fake.writes('GET', PORTFOLIO_SUMMARY_PATH).length).toBeGreaterThan(0);
    });
    expect(fake.writes('GET', PORTFOLIO_SUMMARY_PATH).map((r) => new URL(r.url).search)).toEqual(
      fake.writes('GET', PORTFOLIO_SUMMARY_PATH).map(() => '?portfolio=2'),
    );
  });

  it('falls back to all when the remembered portfolio no longer exists', async () => {
    const fake = fakePortfolio(healthyPortfolio());
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '9');
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/']);

    await waitFor(() => {
      expect(switcher()).toHaveValue('all');
    });
    expect(
      fake.writes('GET', PORTFOLIO_SUMMARY_PATH).every((r) => !r.url.includes('portfolio=')),
    ).toBe(true);
  });
});

describe('wallets', () => {
  it('files a new wallet in the portfolio picked in the form', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '2');
    const fake = fakePortfolio({ wallets: [] });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/wallets']);

    const form = await screen.findByRole('form', { name: 'Add a wallet' });
    const field = await within(form).findByRole('combobox', { name: 'Portfolio' });
    // It follows the selection at the top until the owner picks another.
    expect(field).toHaveValue('2');
    await user.selectOptions(field, 'Main');
    await user.type(within(form).getByLabelText('Address'), ADDRESSES.btcSegwit);
    await user.click(within(form).getByRole('button', { name: 'Add wallet' }));

    await waitFor(() => {
      expect(fake.writes('POST', WALLETS_PATH)).toHaveLength(1);
    });
    expect(fake.writes('POST', WALLETS_PATH)[0]?.body).toMatchObject({ portfolio_id: 1 });
  });

  it('ignores a choice that is not one of the portfolios', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio({ wallets: [] });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/wallets']);

    const form = await screen.findByRole('form', { name: 'Add a wallet' });
    const field = await within(form).findByRole('combobox', { name: 'Portfolio' });
    fireEvent.change(field, { target: { value: 'not-a-portfolio' } });
    await user.type(within(form).getByLabelText('Address'), ADDRESSES.btcSegwit);
    await user.click(within(form).getByRole('button', { name: 'Add wallet' }));

    await waitFor(() => {
      expect(fake.writes('POST', WALLETS_PATH)).toHaveLength(1);
    });
    expect(fake.writes('POST', WALLETS_PATH)[0]?.body).toMatchObject({ portfolio_id: 1 });
  });

  it('sends no portfolio with one portfolio, and shows no field for it', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio({ wallets: [] });
    signIn(fakePortfolios([MAIN]));
    server.use(...fake.handlers);
    renderApp(['/wallets']);

    const form = await screen.findByRole('form', { name: 'Add a wallet' });
    await user.type(within(form).getByLabelText('Address'), ADDRESSES.btcSegwit);
    await user.click(within(form).getByRole('button', { name: 'Add wallet' }));

    await waitFor(() => {
      expect(fake.writes('POST', WALLETS_PATH)).toHaveLength(1);
    });
    expect(within(form).queryByRole('combobox', { name: 'Portfolio' })).not.toBeInTheDocument();
    expect(fake.writes('POST', WALLETS_PATH)[0]?.body).not.toHaveProperty('portfolio_id');
  });

  it('moves a wallet to another portfolio from its row', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio({
      wallets: [wallet({ id: 7, address: ADDRESSES.btcSegwit, label: 'Cold storage' })],
    });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    renderApp(['/wallets']);

    const select = await screen.findByRole('combobox', { name: 'Portfolio of Cold storage' });
    expect(select).toHaveValue('1');
    await user.selectOptions(select, 'Trading');

    await waitFor(() => {
      expect(fake.wallets()[0]?.portfolio_id).toBe(2);
    });
    expect(fake.writes('PATCH', `${WALLETS_PATH}/7`)[0]?.body).toEqual({ portfolio_id: 2 });
  });
  it('says so when a wallet could not be moved, and ignores a choice that is no portfolio', async () => {
    const user = userEvent.setup();
    const fake = fakePortfolio({
      wallets: [wallet({ id: 7, address: ADDRESSES.btcSegwit, label: 'Cold storage' })],
    });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...fake.handlers);
    server.use(
      http.patch(WALLET_PATH, () => problem(404, 'Not Found', 'No portfolio with that id.')),
    );
    renderApp(['/wallets']);

    const select = await screen.findByRole('combobox', { name: 'Portfolio of Cold storage' });
    fireEvent.change(select, { target: { value: 'not-a-portfolio' } });
    expect(fake.writes('PATCH', `${WALLETS_PATH}/7`)).toEqual([]);
    await user.selectOptions(select, 'Trading');

    expect(await screen.findByRole('alert')).toHaveTextContent('No portfolio with that id.');
  });
});

describe('operations', () => {
  it('moves what the filters keep in the chosen portfolio to another', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '1');
    const operations = fakeOperations({
      operations: [
        operation({ id: 1, asset: 'BTC' }),
        operation({ id: 2, asset: 'KAS', external_id: 'b', quantity: '100' }),
        operation({ id: 3, asset: 'BTC', external_id: 'c', portfolio_id: 2 }),
      ],
    });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...operations.handlers, ...fakePortfolio(healthyPortfolio()).handlers);
    renderApp(['/operations']);

    const group = await screen.findByRole('group', { name: 'Move to another portfolio' });
    const target = within(group).getByRole('combobox', { name: 'Move these 2 operations to' });
    // Only the other portfolios are offered.
    expect(
      within(target)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Trading']);
    await user.click(within(group).getByRole('button', { name: 'Move' }));

    expect(await within(group).findByRole('status')).toHaveTextContent('Moved 2 operations.');
    const sent = operations.requests.find((request) => request.url.endsWith('/move'));
    expect(sent?.body).toEqual({
      portfolio_id: 2,
      from_portfolio: 1,
      asset: null,
      venue: null,
      since: null,
      until: null,
    });
  });

  it('shows which portfolio each operation is in when the table shows them all', async () => {
    const operations = fakeOperations({
      operations: [operation({ id: 1 }), operation({ id: 3, external_id: 'c', portfolio_id: 2 })],
    });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...operations.handlers, ...fakePortfolio(healthyPortfolio()).handlers);
    renderApp(['/operations']);

    const table = await screen.findByRole('table', { name: 'Stored operations, newest first' });
    expect(within(table).getByRole('columnheader', { name: 'Portfolio' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: 'Trading' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: 'Main' })).toBeInTheDocument();
  });
  it('moves them to the portfolio picked, not only the first one offered', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(SELECTED_PORTFOLIO_KEY, '1');
    const operations = fakeOperations({ operations: [operation({ id: 1 })] });
    signIn(fakePortfolios([MAIN, TRADING, RETIREMENT]));
    server.use(...operations.handlers, ...fakePortfolio(healthyPortfolio()).handlers);
    renderApp(['/operations']);

    const group = await screen.findByRole('group', { name: 'Move to another portfolio' });
    await user.selectOptions(
      within(group).getByRole('combobox', { name: 'Move these 1 operation to' }),
      'Retirement',
    );
    await user.click(within(group).getByRole('button', { name: 'Move' }));

    expect(await within(group).findByRole('status')).toHaveTextContent('Moved 1 operation.');
    const sent = operations.requests.find((request) => request.url.endsWith('/move'));
    expect(sent?.body).toMatchObject({ portfolio_id: 3, from_portfolio: 1 });
  });

  it('uploads a report into the portfolio picked in the form', async () => {
    const user = userEvent.setup();
    const report: ImportReport = {
      filename: 'bitget.csv',
      stored: 0,
      already_stored: 0,
      files: [],
      portfolio_id: 2,
    };
    const operations = fakeOperations({ uploads: [report] });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...operations.handlers, ...fakePortfolio(healthyPortfolio()).handlers);
    renderApp(['/operations']);

    const form = await screen.findByRole('form', { name: 'Upload exchange reports' });
    await user.selectOptions(
      await within(form).findByRole('combobox', { name: 'Portfolio' }),
      'Trading',
    );
    await user.upload(
      within(form).getByLabelText('Files'),
      new File(['a,b\n1,2\n'], 'bitget.csv', { type: 'text/csv' }),
    );
    await user.click(within(form).getByRole('button', { name: 'Upload' }));

    await waitFor(() => {
      expect(operations.requests.some((request) => request.url.endsWith(IMPORTS_PATH))).toBe(true);
    });
    const sent = operations.requests.find((request) => request.url.endsWith(IMPORTS_PATH));
    expect(sent?.body).toMatchObject({ filename: 'bitget.csv', portfolio_id: 2 });
  });
  it('moves only what the filters keep, and says so when the move is refused', async () => {
    const user = userEvent.setup();
    const operations = fakeOperations({
      operations: [operation({ id: 1 }), operation({ id: 2, asset: 'KAS', external_id: 'b' })],
    });
    signIn(fakePortfolios([MAIN, TRADING]));
    server.use(...operations.handlers, ...fakePortfolio(healthyPortfolio()).handlers);
    let sent: unknown;
    server.use(
      http.post('/api/exchange-operations/move', async ({ request }) => {
        sent = await request.json();
        return problem(404, 'Not Found', 'No portfolio with that id.');
      }),
    );
    renderApp(['/operations']);

    const filters = await screen.findByRole('group', { name: 'Filters' });
    await user.selectOptions(within(filters).getByLabelText('Asset'), 'BTC');
    await user.selectOptions(within(filters).getByLabelText('Where'), 'Bitget');
    fireEvent.change(within(filters).getByLabelText('From'), { target: { value: '2026-05-01' } });
    fireEvent.change(within(filters).getByLabelText('To'), { target: { value: '2026-05-31' } });
    const group = await screen.findByRole('group', { name: 'Move to another portfolio' });
    await user.click(within(group).getByRole('button', { name: 'Move' }));

    expect(await within(group).findByRole('alert')).toHaveTextContent(
      'Not moved: No portfolio with that id.',
    );
    // All portfolios are shown, so every one is a destination and the first is the default.
    expect(sent).toMatchObject({
      portfolio_id: 1,
      from_portfolio: null,
      asset: 'BTC',
      venue: 'Bitget',
    });
    expect(sent).toHaveProperty('since', expect.stringMatching(/^2026-0[45]-/));
    expect(sent).toHaveProperty('until', expect.stringMatching(/^2026-0[56]-/));
  });
});
