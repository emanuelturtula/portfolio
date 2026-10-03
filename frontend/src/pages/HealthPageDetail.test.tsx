import { screen, waitFor, within } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import type {
  ChainHealth,
  ExchangeHealth,
  PricesHealth,
  ReconciliationHealth,
  SchedulerStatus,
} from '@/api/health';
import type { components } from '@/api/generated/schema';
import { HealthPage } from '@/pages/HealthPage';
import { HEALTH_DETAIL_PATH, healthDetail, okBackup, serveDetail } from '@/test/backupFixtures';
import {
  chain,
  emptyChains,
  emptyExchanges,
  exchange,
  freshPrices,
  neverPrices,
  notComputedReconciliation,
  reconciliation,
  stalePrices,
  timer,
  unavailableChains,
  unavailableExchanges,
  unavailablePrices,
  unavailableReconciliation,
  type HealthSections,
} from '@/test/healthFixtures';
import { renderWithProviders } from '@/test/render';
import { problem, server } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The Health page's sections after Backups (spec 030): the timers, the balance sync per chain,
 * the exchange accounts, the prices and the reconciliation. Every state of every section is
 * rendered through the page, from `GET /api/health/detail` as the backend serves it, and read
 * as the owner reads it -- each `<dt>` with the `<dd>` that follows it, each item under its
 * `h4` -- so a value under the wrong label, or in the wrong item, fails.
 */

const UNAVAILABLE = 'Could not be read. The log says why.';
const SECTION_TITLES = ['Timers', 'Balance sync', 'Exchanges', 'Prices', 'Reconciliation'];

function renderDetail(sections: Partial<HealthSections> = {}) {
  const served = serveDetail(healthDetail(okBackup, sections));
  server.use(served.handler);
  renderWithProviders(<HealthPage />);
  return served;
}

async function section(name: string): Promise<HTMLElement> {
  return screen.findByRole('region', { name });
}

/** Every label in `container` with its value, in order, whitespace made plain. */
function rows(container: HTMLElement): [string, string][] {
  const terms = Array.from(container.querySelectorAll('dt'));
  return terms.map((term) => [
    term.textContent,
    (term.nextElementSibling?.textContent ?? '').replace(/\s+/gu, ' '),
  ]);
}

/** Each item of a section: its `h4`, and the rows of the list under it. */
function items(container: HTMLElement): [string, [string, string][]][] {
  return Array.from(container.querySelectorAll('h4')).map((heading) => [
    heading.textContent,
    rows(heading.parentElement ?? container),
  ]);
}

/** A section once it has answered: the first `dt`, an empty state or an alert is in it. */
async function answered(name: string): Promise<HTMLElement> {
  const found = await section(name);
  await waitFor(() => {
    expect(found.querySelector('dt, .state')).not.toBeNull();
  });
  return found;
}

describe('HealthPage: the detail sections, as a whole', () => {
  it('announces one loading line until the detail answers', async () => {
    server.use(
      http.get(HEALTH_DETAIL_PATH, async () => {
        await delay('infinite');
        return new Response(null);
      }),
    );
    renderWithProviders(<HealthPage />);

    const loading = await screen.findByText('Loading the timers, sources and reconciliation...');

    expect(loading).toHaveAttribute('role', 'status');
    for (const title of SECTION_TITLES) {
      expect(screen.queryByRole('region', { name: title })).not.toBeInTheDocument();
    }
  });

  it('shows every section in order, each a landmark named by its h3, after Backups', async () => {
    renderDetail();

    await answered('Reconciliation');
    const regions = screen
      .getAllByRole('region')
      .map((region) => region.getAttribute('aria-labelledby'));
    const names = regions.map((id) => document.getElementById(id ?? '')?.textContent);

    expect(names).toEqual(['Backend health', 'Backups', ...SECTION_TITLES]);
    for (const title of SECTION_TITLES) {
      expect(screen.getByRole('heading', { name: title, level: 3 })).toBeVisible();
    }
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('reports a failed request once, as an h3 alert beside Backups own, and no section', async () => {
    server.use(
      http.get(HEALTH_DETAIL_PATH, () =>
        problem(500, 'Internal Server Error', 'The detail could not be read.'),
      ),
    );
    renderWithProviders(<HealthPage />);

    await waitFor(() => {
      expect(screen.getAllByRole('alert')).toHaveLength(2);
    });
    const backups = within(await section('Backups')).getByRole('alert');
    const [, detail] = screen.getAllByRole('alert');

    expect(within(backups).getByRole('heading', { level: 4 })).toHaveTextContent(
      'Could not load the backup status',
    );
    expect(detail).not.toBe(backups);
    expect(
      screen.getByRole('heading', {
        level: 3,
        name: 'Could not load the timers, sources and reconciliation',
      }),
    ).toBeVisible();
    expect(detail).toHaveTextContent('The detail could not be read.');
    for (const title of SECTION_TITLES) {
      expect(screen.queryByRole('region', { name: title })).not.toBeInTheDocument();
    }
    expect(screen.queryByText(/^OK\./u)).not.toBeInTheDocument();
  });

  it('says the backend could not be reached when the request never got an answer', async () => {
    server.use(http.get(HEALTH_DETAIL_PATH, () => HttpResponse.error()));
    renderWithProviders(<HealthPage />);

    await waitFor(() => {
      expect(screen.getAllByRole('alert')).toHaveLength(2);
    });

    expect(screen.getAllByRole('alert')[1]).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('shows a section that could not be read as an alert while the others answer', async () => {
    renderDetail({ chains: unavailableChains });

    const chains = await answered('Balance sync');
    await answered('Reconciliation');

    expect(within(chains).getByRole('alert')).toHaveTextContent(UNAVAILABLE);
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(items(await section('Exchanges'))).toHaveLength(1);
    expect(items(await section('Timers'))).toHaveLength(4);
  });

  it('shows every readable section beside four that could not be read', async () => {
    renderDetail({
      chains: unavailableChains,
      exchanges: unavailableExchanges,
      prices: unavailablePrices,
      reconciliation: unavailableReconciliation,
    });

    await answered('Reconciliation');

    for (const title of ['Balance sync', 'Exchanges', 'Prices', 'Reconciliation']) {
      const found = await section(title);
      expect(within(found).getByRole('alert')).toHaveTextContent(UNAVAILABLE);
      expect(within(found).queryByRole('term')).not.toBeInTheDocument();
    }
    expect(items(await section('Timers'))).toHaveLength(4);
  });
});

describe('HealthPage: the timers', () => {
  it('names the four timers in the order served, each with its state, tick and result', async () => {
    inTimeZone('UTC');
    renderDetail();

    const timers = await answered('Timers');

    expect(items(timers)).toEqual(
      ['Balance sync', 'Price refresh', 'Exchange sync', 'Backup'].map((name) => [
        name,
        [
          ['State', 'OK. The timer is running on schedule.'],
          ['Last tick', 'Oct 3, 2026, 11:45 AM'],
          ['Last result', 'Succeeded.'],
        ],
      ]),
    );
    expect(timers.querySelector('time')).toHaveAttribute('datetime', '2026-10-03T11:45:00Z');
  });

  it.each([
    ['late', 'Late. The timer is running but has not finished a tick when it should have.'],
    ['stopped', 'Stopped. The timer is not running.'],
  ] as const)('words a %s timer', async (state, words) => {
    renderDetail({ schedulers: [timer({ state })] });

    expect(rows(await answered('Timers'))[0]).toEqual(['State', words]);
  });

  it('says how a failed tick ended', async () => {
    renderDetail({ schedulers: [timer({ last_tick_succeeded: false })] });

    expect(rows(await answered('Timers'))[2]).toEqual(['Last result', 'Failed. The log says why.']);
  });

  it('says a timer has not ticked since the server started, with no result', async () => {
    renderDetail({ schedulers: [timer({ last_tick_at: null, last_tick_succeeded: null })] });

    expect(rows(await answered('Timers'))).toEqual([
      ['State', 'OK. The timer is running on schedule.'],
      ['Last tick', 'none since the server started'],
    ]);
  });

  it('shows a disabled timer by its state only (R11)', async () => {
    renderDetail({
      schedulers: [timer({ state: 'disabled', last_tick_at: null, last_tick_succeeded: null })],
    });

    const timers = await answered('Timers');

    expect(rows(timers)).toEqual([
      ['State', 'Disabled. This timer is switched off on this server.'],
    ]);
    expect(within(timers).queryByText('Last tick')).not.toBeInTheDocument();
    expect(within(timers).queryByText('none since the server started')).not.toBeInTheDocument();
  });

  it('shows a disabled timer by its state only even if a tick were served', async () => {
    renderDetail({ schedulers: [timer({ state: 'disabled' })] });

    expect(rows(await answered('Timers'))).toHaveLength(1);
  });

  it('tells a disabled timer apart from a stopped one', async () => {
    const both: SchedulerStatus[] = [
      timer({ name: 'backup', state: 'disabled', last_tick_at: null, last_tick_succeeded: null }),
      timer({
        name: 'exchange-sync',
        state: 'stopped',
        last_tick_at: null,
        last_tick_succeeded: null,
      }),
    ];
    renderDetail({ schedulers: both });

    expect(items(await answered('Timers'))).toEqual([
      ['Backup', [['State', 'Disabled. This timer is switched off on this server.']]],
      [
        'Exchange sync',
        [
          ['State', 'Stopped. The timer is not running.'],
          ['Last tick', 'none since the server started'],
        ],
      ],
    ]);
  });
});

describe('HealthPage: the balance sync per chain', () => {
  it('shows a chain read by the last sync, and when', async () => {
    inTimeZone('UTC');
    renderDetail();

    expect(items(await answered('Balance sync'))).toEqual([
      [
        'Bitcoin',
        [
          ['State', 'OK. The last balance sync read this chain.'],
          ['Last success', 'Oct 3, 2026, 11:30 AM'],
        ],
      ],
    ]);
  });

  it.each([
    ['unavailable', 'The provider could not be reached.'],
    ['rate_limited', 'The provider is rate-limiting requests.'],
    ['response', 'The provider sent a response that could not be used.'],
    ['unknown_chain', 'This chain is not configured on the server.'],
    [
      'address_rejected',
      'An address on this chain was refused, so none of its wallets were read. Check that every address belongs to the network this server reads.',
    ],
    ['internal', 'An internal error interrupted the read; see the server log.'],
  ] as const)('names a %s failure in words', async (kind, words) => {
    inTimeZone('UTC');
    renderDetail({
      chains: { state: 'ok', items: [chain({ state: 'failing', last_error_kind: kind })] },
    });

    expect(rows(await answered('Balance sync'))).toEqual([
      ['State', 'Failing. The last balance sync could not read this chain.'],
      ['Last success', 'Oct 3, 2026, 11:30 AM'],
      ['Last failure', words],
    ]);
  });

  it('says never for a chain no sync has read, and for one that never succeeded', async () => {
    const chains: ChainHealth[] = [
      chain({
        chain_key: 'bitcoin',
        state: 'failing',
        last_success_at: null,
        last_error_kind: 'internal',
      }),
      chain({ chain_key: 'kaspa', state: 'never', last_success_at: null }),
    ];
    renderDetail({ chains: { state: 'ok', items: chains } });

    expect(items(await answered('Balance sync'))).toEqual([
      [
        'Bitcoin',
        [
          ['State', 'Failing. The last balance sync could not read this chain.'],
          ['Last success', 'never'],
          ['Last failure', 'An internal error interrupted the read; see the server log.'],
        ],
      ],
      [
        'Kaspa',
        [
          ['State', 'Never read. No finished balance sync has an outcome for this chain yet.'],
          ['Last success', 'never'],
        ],
      ],
    ]);
  });

  it('shows a chain key it has no name for as the key itself', async () => {
    renderDetail({ chains: { state: 'ok', items: [chain({ chain_key: 'litecoin' })] } });

    expect(items(await answered('Balance sync'))[0]?.[0]).toBe('litecoin');
  });

  it('tells an empty list apart from a section that could not be read', async () => {
    renderDetail({ chains: emptyChains });

    const chains = await answered('Balance sync');

    expect(within(chains).getByRole('heading', { level: 4 })).toHaveTextContent(
      'No chains to report yet',
    );
    expect(chains).toHaveTextContent(
      'Add a wallet and the balance sync will report on its chain here.',
    );
    expect(within(chains).queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('HealthPage: the exchange accounts', () => {
  it('shows the trade sync and the balance read apart, each with its instant (R4)', async () => {
    inTimeZone('UTC');
    renderDetail();

    expect(items(await answered('Exchanges'))).toEqual([
      [
        'Bitget',
        [
          ['Trade sync', 'OK. The last trade sync finished.'],
          ['Last successful sync', 'Oct 3, 2026, 10:15 AM'],
          ['Balances', 'OK. The last balance read succeeded.'],
          ['Balances read', 'Oct 3, 2026, 10:16 AM'],
        ],
      ],
    ]);
  });

  it.each([
    ['error', 'Failing. The last trade sync failed.'],
    [
      'auth_failed',
      'Authentication failed. The exchange refused the API key. Scheduled syncs skip this account until a manual sync succeeds.',
    ],
    ['never_synced', 'Never synced. No trade sync has finished for this account yet.'],
  ] as const)('words a trade sync that is %s', async (syncState, words) => {
    renderDetail({ exchanges: { state: 'ok', items: [exchange({ sync_state: syncState })] } });

    expect(rows(await answered('Exchanges'))[0]).toEqual(['Trade sync', words]);
  });

  it.each([
    ['failing', 'Failing. The last balance read failed.'],
    ['never', 'Never read. No balance has been read for this account yet.'],
  ] as const)('words a balance read that is %s', async (balancesState, words) => {
    renderDetail({
      exchanges: { state: 'ok', items: [exchange({ balances_state: balancesState })] },
    });

    expect(rows(await answered('Exchanges'))[2]).toEqual(['Balances', words]);
  });

  it('says never for an account never synced and never read, under each venue name', async () => {
    const accounts: ExchangeHealth[] = [
      exchange({
        exchange_key: 'bingx',
        sync_state: 'never_synced',
        last_synced_at: null,
        balances_state: 'never',
        balances_read_at: null,
      }),
      exchange({ exchange_key: 'bitget' }),
    ];
    renderDetail({ exchanges: { state: 'ok', items: accounts } });

    const found = items(await answered('Exchanges'));

    expect(found.map(([name]) => name)).toEqual(['BingX', 'Bitget']);
    expect(found[0]?.[1]).toEqual([
      ['Trade sync', 'Never synced. No trade sync has finished for this account yet.'],
      ['Last successful sync', 'never'],
      ['Balances', 'Never read. No balance has been read for this account yet.'],
      ['Balances read', 'never'],
    ]);
  });

  it('tells an empty list apart from a section that could not be read', async () => {
    renderDetail({ exchanges: emptyExchanges });

    const exchanges = await answered('Exchanges');

    expect(within(exchanges).getByRole('heading', { level: 4 })).toHaveTextContent(
      'No exchange accounts to report yet',
    );
    expect(exchanges).toHaveTextContent('No exchange account is set up on this server.');
    expect(within(exchanges).queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('HealthPage: the prices', () => {
  it.each([
    [freshPrices, 'Fresh. Prices were fetched recently.', 'Oct 3, 2026, 11:50 AM'],
    [
      stalePrices,
      'Stale. Prices have not been fetched recently, so values that use them may be out of date.',
      'Oct 3, 2026, 11:50 AM',
    ],
    [neverPrices, 'Never fetched. No price has been fetched yet.', 'never'],
  ] as [PricesHealth, string, string][])('words %o', async (prices, words, latest) => {
    inTimeZone('UTC');
    renderDetail({ prices });

    expect(rows(await answered('Prices'))).toEqual([
      ['State', words],
      ['Latest fetch', latest],
    ]);
  });
});

describe('HealthPage: the reconciliation', () => {
  type ReconciliationState = components['schemas']['ReconciliationHealthState'];

  it('shows the state, when it was computed and three counts', async () => {
    inTimeZone('UTC');
    renderDetail();

    expect(rows(await answered('Reconciliation'))).toEqual([
      ['State', 'Match. Every asset compared agrees with the balances read.'],
      ['Computed', 'Oct 3, 2026, 9:05 AM'],
      ['Assets compared', '4'],
      ['Assets that differ', '0'],
      ['Sources not compared', '0'],
    ]);
  });

  it.each([
    [
      'mismatch',
      'Mismatch. At least one asset does not agree with the balances read.',
      { assets_compared: 1234, assets_mismatched: 2, sources_not_compared: 0 },
      ['1,234', '2', '0'],
    ],
    [
      'incomplete',
      'Incomplete. A source could not be compared, so the check does not cover everything.',
      { assets_compared: 3, assets_mismatched: 0, sources_not_compared: 2 },
      ['3', '0', '2'],
    ],
  ] as const)('words a %s and its counts', async (state, words, counts, shown) => {
    renderDetail({ reconciliation: reconciliation({ state, ...counts }) });

    const found = rows(await answered('Reconciliation'));

    expect(found[0]).toEqual(['State', words]);
    expect(found.slice(2)).toEqual([
      ['Assets compared', shown[0]],
      ['Assets that differ', shown[1]],
      ['Sources not compared', shown[2]],
    ]);
  });

  it('shows no count for a check that was never computed', async () => {
    renderDetail({ reconciliation: notComputedReconciliation });

    expect(rows(await answered('Reconciliation'))).toEqual([
      ['State', 'Not computed. The positions have not been computed yet.'],
      ['Computed', 'never'],
    ]);
  });

  it.each<[ReconciliationState, ReconciliationHealth]>([
    ['match', reconciliation()],
    ['mismatch', reconciliation({ state: 'mismatch', assets_mismatched: 1 })],
    ['incomplete', reconciliation({ state: 'incomplete', sources_not_compared: 1 })],
    ['not_computed', notComputedReconciliation],
    ['unavailable', unavailableReconciliation],
  ])('links to the holdings check on the dashboard when it is %s', async (_state, served) => {
    renderDetail({ reconciliation: served });

    const found = await answered('Reconciliation');
    const link = within(found).getByRole('link', {
      name: 'See the holdings check on the dashboard',
    });

    expect(link).toHaveAttribute('href', '/#holdings-check');
  });

  it('shows a check that could not be read as an alert, with no state and no count', async () => {
    renderDetail({ reconciliation: unavailableReconciliation });

    const found = await answered('Reconciliation');

    expect(within(found).getByRole('alert')).toHaveTextContent(UNAVAILABLE);
    expect(rows(found)).toEqual([]);
  });
});
