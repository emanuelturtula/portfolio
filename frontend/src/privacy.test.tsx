import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { fakePortfolio, recordRequestUrls } from '@/test/fakePortfolio';
import { ADDRESSES, ALL_ADDRESSES, healthyPortfolio, wallet } from '@/test/fixtures';
import { renderApp, settle, visitedPaths } from '@/test/render';
import { fakeSession, server, TEST_USERNAME } from '@/test/server';

/**
 * The action half of a per-row control's accessible name (R8). Every row's
 * control is named for its row - "Archive Cold storage" - and these match the
 * action within one row; the tests under "row names" pin the full names.
 */
const ARCHIVE = /^Archive /;
const CONFIRM_ARCHIVE = /^Confirm archive of /;
const RESTORE = /^Restore /;
const COPY_ADDRESS = /^Copy address of /;

/**
 * Every form an address could take in a URL: as typed, percent-encoded, and
 * form-encoded. `kaspatest:` encodes its colon, so a check for the raw string
 * alone would miss exactly the address most likely to be put in a query.
 */
function encodings(address: string): string[] {
  return [
    address,
    encodeURIComponent(address),
    new URLSearchParams({ a: address }).toString().slice(2),
  ];
}

const CONSOLE_METHODS = ['log', 'info', 'warn', 'error', 'debug'] as const;

describe('privacy', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('no request URL carries an address', async () => {
    const user = userEvent.setup();
    const scenario = healthyPortfolio();
    const archived = wallet({
      id: 4,
      chain_key: 'bitcoin',
      address: ADDRESSES.btcScript,
      label: 'Old exchange',
      archived: true,
    });
    const fake = fakePortfolio({ ...scenario, wallets: [...scenario.wallets, archived] });
    fake.rejectAddress(ADDRESSES.btcRegtest, 'bad_checksum');
    server.use(...fakeSession({ initialUser: TEST_USERNAME }).handlers, ...fake.handlers);
    const consoleCalls = CONSOLE_METHODS.map((method) => vi.spyOn(console, method));
    const urls = recordRequestUrls();

    renderApp(['/wallets']);

    // Wallets page: list, show archived, restore, archive, a refused add, a
    // duplicate add and a successful one - every request the page can make.
    const region = await screen.findByRole('region', { name: 'Your wallets' });
    await within(region).findByRole('list');
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }));
    const oldExchange = await within(region).findByText('Old exchange');
    const archivedRow = oldExchange.closest('li');
    if (archivedRow === null) {
      throw new Error('The archived wallet is not in a list item.');
    }
    await user.click(within(archivedRow).getByRole('button', { name: RESTORE }));
    await waitFor(() => {
      expect(fake.wallets().find((entry) => entry.id === 4)?.archived).toBe(false);
    });

    const coldRow = within(region).getByText('Cold storage').closest('li');
    if (coldRow === null) {
      throw new Error('The Cold storage wallet is not in a list item.');
    }
    await user.click(within(coldRow).getByRole('button', { name: ARCHIVE }));
    await user.click(within(coldRow).getByRole('button', { name: CONFIRM_ARCHIVE }));
    await waitFor(() => {
      expect(fake.wallets().find((entry) => entry.id === 1)?.archived).toBe(true);
    });

    const form = screen.getByRole('form', { name: 'Add a wallet' });
    const address = within(form).getByLabelText('Address');
    const submit = within(form).getByRole('button', { name: 'Add wallet' });

    await user.type(address, ADDRESSES.btcRegtest);
    await user.click(submit);
    await within(form).findByText('The checksum does not match.');

    await user.clear(address);
    await user.type(address, ADDRESSES.btcLegacy);
    await user.click(submit);
    await within(form).findByText('This address is already registered for this chain.');

    await user.clear(address);
    await user.selectOptions(within(form).getByLabelText('Chain'), 'kaspa');
    await user.type(address, ADDRESSES.kasSecondary);
    await user.click(submit);
    await waitFor(() => {
      expect(fake.wallets().some((entry) => entry.address === ADDRESSES.kasSecondary)).toBe(true);
    });

    // Copying an address is a clipboard write, not a request.
    await user.click(within(archivedRow).getByRole('button', { name: COPY_ADDRESS }));

    // Dashboard: every read, and a refresh.
    const nav = screen.getByRole('navigation', { name: 'Main' });
    await user.click(within(nav).getByRole('link', { name: 'Dashboard' }));
    await screen.findByRole('region', { name: 'Total value' });
    await user.click(screen.getByRole('button', { name: 'Refresh' }));
    await waitFor(() => {
      expect(fake.writes('POST', '/api/balances/sync')).toHaveLength(1);
    });
    await settle();

    // The positive control: the recorder saw the flow, including the writes
    // addressed by id. An empty list would pass the assertion below for the
    // wrong reason.
    const paths = urls.map((url) => `${new URL(url).pathname}${new URL(url).search}`);
    expect(paths).toContain('/api/wallets?include_archived=true');
    expect(paths).toContain('/api/wallets/4');
    expect(paths).toContain('/api/wallets/1');
    expect(paths).toContain('/api/balances/current');
    expect(paths).toContain('/api/balances/runs?limit=2');
    expect(paths).toContain('/api/balances/sync');
    expect(fake.writes('POST', '/api/wallets')).toHaveLength(3);

    for (const url of urls) {
      for (const candidate of ALL_ADDRESSES) {
        for (const encoded of encodings(candidate)) {
          expect(url).not.toContain(encoded);
        }
      }
    }

    // Nor in a route.
    for (const path of visitedPaths()) {
      for (const candidate of ALL_ADDRESSES) {
        for (const encoded of encodings(candidate)) {
          expect(path).not.toContain(encoded);
        }
      }
    }

    // Nor in a log line.
    for (const spy of consoleCalls) {
      for (const call of spy.mock.calls) {
        const text = call.map((argument) => String(argument)).join(' ');
        for (const candidate of ALL_ADDRESSES) {
          expect(text).not.toContain(candidate);
        }
      }
    }
  });
});
