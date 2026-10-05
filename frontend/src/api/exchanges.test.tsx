import { QueryClientProvider, useQuery, type QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  positionsQueryKey,
  reconciliationQueryKey,
  usePositions,
  useReconciliation,
} from '@/api/accounting';
import {
  EXCHANGE_RUNS_LIMIT,
  exchangeFillsQueryKey,
  exchangeRunsQueryKey,
  exchangesQueryKey,
  FAST_POLL_MS,
  listRefetchInterval,
  runsRefetchInterval,
  SLOW_POLL_MS,
  useExchangeFills,
  useExchangeRuns,
  useExchanges,
  useSyncExchanges,
} from '@/api/exchanges';
import { NO_FILTERS, type FillFilters } from '@/lib/fillFilters';
import { createQueryClient } from '@/lib/queryClient';
import { emptySnapshot, investedPortfolio } from '@/test/accountingFixtures';
import {
  accountFailed,
  authFailedExchange,
  exchange,
  finishedRun,
  interruptedExchangeRun,
  NOW,
  runningExchangeRun,
  unsyncedExchange,
} from '@/test/exchangeFixtures';
import { fakeAccounting } from '@/test/fakeAccounting';
import { fakeExchanges, type FakeExchanges } from '@/test/fakeExchanges';
import { manyFills } from '@/test/fillFixtures';
import { kasNeverTraded, reconciliation, walletReadings } from '@/test/reconciliationFixtures';
import { settle } from '@/test/render';
import { problem, server } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

afterEach(() => {
  vi.useRealTimers();
});

function wrapperFor(client: QueryClient) {
  return function Wrapper({ children }: { readonly children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe('the polling constants', () => {
  it('polls every 5 seconds fast, every minute slow, and reads 20 runs', () => {
    expect(FAST_POLL_MS).toBe(5_000);
    expect(SLOW_POLL_MS).toBe(60_000);
    expect(EXCHANGE_RUNS_LIMIT).toBe(20);
  });

  it('keys both queries under exchanges, so one invalidation reaches both', () => {
    expect(exchangesQueryKey).toEqual(['exchanges', 'list']);
    expect(exchangeRunsQueryKey).toEqual(['exchanges', 'runs']);
  });
});

describe('listRefetchInterval', () => {
  it('polls slowly before the first read, unless this page is syncing', () => {
    expect(listRefetchInterval(undefined, false)).toBe(SLOW_POLL_MS);
    expect(listRefetchInterval(undefined, true)).toBe(FAST_POLL_MS);
  });

  it('polls slowly when no venue is syncing', () => {
    expect(listRefetchInterval([], false)).toBe(SLOW_POLL_MS);
    expect(listRefetchInterval([exchange()], false)).toBe(SLOW_POLL_MS);
    expect(
      listRefetchInterval([unsyncedExchange('bingx'), authFailedExchange('auth')], false),
    ).toBe(SLOW_POLL_MS);
  });

  it('polls fast while any venue is syncing', () => {
    // A venue without credentials is never syncing, so "any" and "every"
    // differ exactly when one venue has been unconfigured.
    const syncing = exchange({ syncing: true });
    const unconfigured = exchange({ exchange_key: 'bingx', configured: false });

    expect(listRefetchInterval([syncing], false)).toBe(FAST_POLL_MS);
    expect(listRefetchInterval([unconfigured, syncing], false)).toBe(FAST_POLL_MS);
    expect(listRefetchInterval([syncing, unconfigured], false)).toBe(FAST_POLL_MS);
  });

  it("polls fast while this page's own sync is pending, whatever the list says", () => {
    expect(listRefetchInterval([], true)).toBe(FAST_POLL_MS);
    expect(listRefetchInterval([exchange()], true)).toBe(FAST_POLL_MS);
    expect(listRefetchInterval([exchange({ syncing: true })], true)).toBe(FAST_POLL_MS);
  });
});

describe('runsRefetchInterval', () => {
  it('polls slowly before the first read, unless this page is syncing', () => {
    expect(runsRefetchInterval(undefined, false, false)).toBe(SLOW_POLL_MS);
    expect(runsRefetchInterval(undefined, false, true)).toBe(SLOW_POLL_MS);
    expect(runsRefetchInterval(undefined, true, false)).toBe(FAST_POLL_MS);
  });

  it('polls slowly when the log is empty or its newest run has ended', () => {
    for (const anySyncing of [false, true]) {
      expect(runsRefetchInterval([], false, anySyncing)).toBe(SLOW_POLL_MS);
      expect(runsRefetchInterval([finishedRun()], false, anySyncing)).toBe(SLOW_POLL_MS);
      expect(runsRefetchInterval([interruptedExchangeRun()], false, anySyncing)).toBe(SLOW_POLL_MS);
      expect(
        runsRefetchInterval([finishedRun(), interruptedExchangeRun()], false, anySyncing),
      ).toBe(SLOW_POLL_MS);
    }
  });

  it('polls fast while the newest run is running and a venue is syncing', () => {
    // A running run is always the newest: opening a run sweeps every older
    // one to interrupted first. So "the newest" is the first entry.
    const running = runningExchangeRun({ accounts_total: 2 });

    expect(runsRefetchInterval([running], false, true)).toBe(FAST_POLL_MS);
    expect(runsRefetchInterval([running, finishedRun()], false, true)).toBe(FAST_POLL_MS);
  });

  it('polls slowly under a running row no venue is syncing: an orphan', () => {
    // R12. `syncing` is the coordinator's in-flight flag. A `running` row
    // without it was left by a failed close-out, and the next run sweeps it.
    const running = runningExchangeRun({ accounts_total: 2 });

    expect(runsRefetchInterval([running], false, false)).toBe(SLOW_POLL_MS);
    expect(runsRefetchInterval([running, finishedRun()], false, false)).toBe(SLOW_POLL_MS);
  });

  it('a venue syncing alone does not speed the log up without a running run', () => {
    expect(runsRefetchInterval([finishedRun()], false, true)).toBe(SLOW_POLL_MS);
  });

  it("polls fast while this page's own sync is pending, whatever the log says", () => {
    for (const anySyncing of [false, true]) {
      expect(runsRefetchInterval([], true, anySyncing)).toBe(FAST_POLL_MS);
      expect(runsRefetchInterval([finishedRun()], true, anySyncing)).toBe(FAST_POLL_MS);
      expect(
        runsRefetchInterval([runningExchangeRun({ accounts_total: 1 })], true, anySyncing),
      ).toBe(FAST_POLL_MS);
    }
  });
});

describe('the exchange hooks', () => {
  function setUp(fake: FakeExchanges = fakeExchanges({ exchanges: [exchange()] })) {
    server.use(...fake.handlers);
    const client = createQueryClient();
    const unrelated = vi.fn(() => Promise.resolve('unrelated'));

    const hook = renderHook(
      () => ({
        list: useExchanges(false),
        runs: useExchangeRuns(false, false),
        // A query outside ['exchanges'], to prove the sync's invalidation is
        // scoped rather than a refetch of everything.
        unrelated: useQuery({ queryKey: ['balances', 'current'], queryFn: unrelated }),
        sync: useSyncExchanges(),
      }),
      { wrapper: wrapperFor(client) },
    );

    return { fake, client, unrelated, hook };
  }

  async function loaded(hook: ReturnType<typeof setUp>['hook']): Promise<void> {
    await waitFor(() => {
      expect(hook.result.current.list.isSuccess).toBe(true);
      expect(hook.result.current.runs.isSuccess).toBe(true);
      expect(hook.result.current.unrelated.isSuccess).toBe(true);
    });
  }

  it('reads the list and the 20 newest runs, under their keys', async () => {
    const { fake, client, hook } = setUp(
      fakeExchanges({ exchanges: [exchange()], runs: [finishedRun()] }),
    );
    await loaded(hook);

    expect(fake.requestsTo('list').map((entry) => new URL(entry.url).pathname)).toEqual([
      '/api/exchanges',
    ]);
    const runs = fake.requestsTo('runs').map((entry) => new URL(entry.url));
    expect(runs).toHaveLength(1);
    expect(runs[0]?.pathname).toBe('/api/exchanges/runs');
    expect(runs[0]?.searchParams.get('limit')).toBe('20');
    expect(client.getQueryData(exchangesQueryKey)).toBeDefined();
    expect(client.getQueryData(exchangeRunsQueryKey)).toBeDefined();
  });

  it('posts the sync as JSON, and re-reads the list and the run log when it succeeds', async () => {
    const { fake, unrelated, hook } = setUp();
    await loaded(hook);
    const listBefore = fake.count('list');
    const runsBefore = fake.count('runs');
    const unrelatedBefore = unrelated.mock.calls.length;

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isSuccess).toBe(true);
    });
    await waitFor(() => {
      expect(fake.count('list')).toBe(listBefore + 1);
      expect(fake.count('runs')).toBe(runsBefore + 1);
    });
    const posts = fake.requestsTo('sync');
    expect(posts).toHaveLength(1);
    expect(posts[0]?.method).toBe('POST');
    // Bodyless, and still declared JSON, or the backend's write guard refuses it.
    expect(posts[0]?.contentType).toBe('application/json');
    await settle();
    expect(unrelated.mock.calls.length).toBe(unrelatedBefore);
  });

  it('re-reads the list and the run log when the sync fails, too', async () => {
    // A proxy that cuts the held request off has not stopped the run: the
    // coordinator shields it. So a failed POST is the moment the page most
    // needs to look again.
    const { fake, unrelated, hook } = setUp();
    await loaded(hook);
    fake.fail('sync', () => problem(504, 'Gateway Timeout', 'The upstream did not answer.'));
    const listBefore = fake.count('list');
    const runsBefore = fake.count('runs');
    const unrelatedBefore = unrelated.mock.calls.length;

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isError).toBe(true);
    });
    await waitFor(() => {
      expect(fake.count('list')).toBe(listBefore + 1);
      expect(fake.count('runs')).toBe(runsBefore + 1);
    });
    await settle();
    expect(unrelated.mock.calls.length).toBe(unrelatedBefore);
  });

  it('hands back the run summary the POST answered with', async () => {
    const run = finishedRun({
      run_id: 12,
      trigger: 'manual',
      accounts: [accountFailed('bitget', 'unavailable')],
    });
    const { hook } = setUp(
      fakeExchanges({ exchanges: [exchange()], onSync: () => ({ ...run, joined: false }) }),
    );
    await loaded(hook);

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.data).toEqual({ ...run, joined: false });
    });
  });
});

describe('the exchange hooks: polling', () => {
  /*
   * The hooks are rendered without the page, so `anySyncing` is fixed here;
   * ExchangesPage.test.tsx covers the page reading it from the list.
   */
  /*
   * `Date` and `setInterval` are faked; `setTimeout` stays real, because MSW
   * answers through it. TanStack Query schedules `refetchInterval` with
   * `setInterval` and restarts it on every query update, so each assertion
   * below counts from the instant the last answer landed.
   */
  function pollingHook(fake: FakeExchanges, syncPending: boolean) {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] });
    vi.setSystemTime(new Date(NOW));
    server.use(...fake.handlers);
    const client = createQueryClient();

    return renderHook(
      ({ pending }) => ({
        list: useExchanges(pending),
        runs: useExchangeRuns(pending, false),
      }),
      { wrapper: wrapperFor(client), initialProps: { pending: syncPending } },
    );
  }

  async function advance(ms: number): Promise<void> {
    act(() => {
      vi.advanceTimersByTime(ms);
    });
    await settle();
  }

  /**
   * `waitFor` cannot be used here: it re-checks on a `setInterval` of its own,
   * which is faked, and on DOM mutations, which a hook's result never makes.
   * So this re-checks on the real `setTimeout` that `settle()` waits on.
   */
  async function eventually(assertion: () => void): Promise<void> {
    for (let attempt = 1; ; attempt += 1) {
      try {
        assertion();
        return;
      } catch (error) {
        if (attempt >= 40) {
          throw error;
        }
        await settle();
      }
    }
  }

  it("polls both queries every 5 seconds while this page's sync is pending", async () => {
    const fake = fakeExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    const hook = pollingHook(fake, true);
    await eventually(() => {
      expect(hook.result.current.list.isSuccess).toBe(true);
      expect(hook.result.current.runs.isSuccess).toBe(true);
    });
    await settle();
    const list = fake.count('list');
    const runs = fake.count('runs');

    await advance(FAST_POLL_MS - 1);
    expect(fake.count('list')).toBe(list);
    expect(fake.count('runs')).toBe(runs);

    await advance(1);
    expect(fake.count('list')).toBe(list + 1);
    expect(fake.count('runs')).toBe(runs + 1);
  });

  it('drops back to one poll a minute when the sync is no longer pending', async () => {
    const fake = fakeExchanges({ exchanges: [exchange()], runs: [finishedRun()] });
    const hook = pollingHook(fake, true);
    await eventually(() => {
      expect(hook.result.current.list.isSuccess).toBe(true);
      expect(hook.result.current.runs.isSuccess).toBe(true);
    });

    hook.rerender({ pending: false });
    await settle();
    const list = fake.count('list');
    const runs = fake.count('runs');

    await advance(FAST_POLL_MS);
    expect(fake.count('list')).toBe(list);
    expect(fake.count('runs')).toBe(runs);

    await advance(SLOW_POLL_MS - FAST_POLL_MS);
    expect(fake.count('list')).toBe(list + 1);
    expect(fake.count('runs')).toBe(runs + 1);
  });
});

/**
 * Spec 022: a sync that stored a fill has already recomputed the position snapshot by the
 * time it answers (spec 021), so settling it re-reads `['accounting', ...]` as well as
 * `['exchanges', ...]`, success or failure.
 */
describe('the exchange sync and the invested figures', () => {
  function setUp(options: { readonly failSync: boolean }) {
    const exchanges = fakeExchanges({ exchanges: [exchange()] });
    const accounting = fakeAccounting({ positions: emptySnapshot() });
    if (options.failSync) {
      exchanges.fail('sync', () => problem(504, 'Gateway Timeout', 'The upstream did not answer.'));
    }
    server.use(...exchanges.handlers, ...accounting.handlers);
    const client = createQueryClient();

    const hook = renderHook(() => ({ positions: usePositions(), sync: useSyncExchanges() }), {
      wrapper: wrapperFor(client),
    });

    return { exchanges, accounting, client, hook };
  }

  it('re-reads the positions when the sync succeeds, and shows the new snapshot', async () => {
    const { accounting, client, hook } = setUp({ failSync: false });
    await waitFor(() => {
      expect(hook.result.current.positions.isSuccess).toBe(true);
    });
    const before = accounting.count();
    // The sync stores fills; the server recomputes before it answers.
    const recomputed = investedPortfolio();
    accounting.setPositions(recomputed);

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isSuccess).toBe(true);
    });
    await waitFor(() => {
      expect(accounting.count()).toBe(before + 1);
    });
    await waitFor(() => {
      expect(hook.result.current.positions.data).toEqual(recomputed);
    });
    expect(client.getQueryData(positionsQueryKey)).toEqual(recomputed);
  });

  it('re-reads the positions when the sync fails, too', async () => {
    // A cut-off request very often means the run is still going: it may yet store a fill.
    const { accounting, hook } = setUp({ failSync: true });
    await waitFor(() => {
      expect(hook.result.current.positions.isSuccess).toBe(true);
    });
    const before = accounting.count();

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isError).toBe(true);
    });
    await waitFor(() => {
      expect(accounting.count()).toBe(before + 1);
    });
  });

  it('marks an inactive positions query stale, so the dashboard re-reads it on return', async () => {
    // On the exchanges page the dashboard is not mounted: nothing observes the positions.
    // The invalidation still reaches the cached entry, so it is not served as fresh later.
    const { client, hook } = setUp({ failSync: false });
    client.setQueryData(['accounting', 'positions', 'inactive-probe'], emptySnapshot());
    await waitFor(() => {
      expect(hook.result.current.positions.isSuccess).toBe(true);
    });

    act(() => {
      hook.result.current.sync.mutate();
    });
    await waitFor(() => {
      expect(hook.result.current.sync.isSuccess).toBe(true);
    });

    await waitFor(() => {
      expect(
        client.getQueryState(['accounting', 'positions', 'inactive-probe'])?.isInvalidated,
      ).toBe(true);
    });
  });
});

/**
 * Spec 025: a venue's balances are read at the end of its successful fill sync, so the
 * comparison the holdings check shows is worth re-reading at the moment the sync settles. It
 * is keyed `['accounting', 'reconciliation']`, under the prefix the sync already invalidates.
 */
describe('the exchange sync and the holdings check', () => {
  /** KAS read in a wallet and never traded: a comparison the first snapshot does not have. */
  function afterTheBalanceRead() {
    return reconciliation({ assets: [kasNeverTraded()], wallets: walletReadings(1) });
  }

  function setUp(options: { readonly failSync: boolean }) {
    const exchanges = fakeExchanges({ exchanges: [exchange()] });
    const accounting = fakeAccounting({ positions: emptySnapshot() });
    if (options.failSync) {
      exchanges.fail('sync', () => problem(504, 'Gateway Timeout', 'The upstream did not answer.'));
    }
    server.use(...exchanges.handlers, ...accounting.handlers);
    const client = createQueryClient();

    const hook = renderHook(
      () => ({ reconciliation: useReconciliation(), sync: useSyncExchanges() }),
      { wrapper: wrapperFor(client) },
    );

    return { accounting, client, hook };
  }

  it('re-reads the reconciliation when the sync succeeds, and shows the new comparison', async () => {
    const { accounting, client, hook } = setUp({ failSync: false });
    await waitFor(() => {
      expect(hook.result.current.reconciliation.isSuccess).toBe(true);
    });
    expect(hook.result.current.reconciliation.data?.assets).toEqual([]);
    const before = accounting.count('reconciliation');
    // The sync reads the venue's balances after its fills; no fill is stored, so the snapshot
    // - and with it the positions - does not move. Only the comparison does.
    accounting.setReconciliation(afterTheBalanceRead());

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isSuccess).toBe(true);
    });
    await waitFor(() => {
      expect(accounting.count('reconciliation')).toBe(before + 1);
    });
    await waitFor(() => {
      expect(hook.result.current.reconciliation.data).toEqual(afterTheBalanceRead());
    });
    expect(client.getQueryData(reconciliationQueryKey)).toEqual(afterTheBalanceRead());
  });

  it('re-reads the reconciliation when the sync fails, too', async () => {
    // A cut-off request very often means the run is still going: it may yet read the balances.
    const { accounting, hook } = setUp({ failSync: true });
    await waitFor(() => {
      expect(hook.result.current.reconciliation.isSuccess).toBe(true);
    });
    const before = accounting.count('reconciliation');

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(hook.result.current.sync.isError).toBe(true);
    });
    await waitFor(() => {
      expect(accounting.count('reconciliation')).toBe(before + 1);
    });
  });

  it('marks an inactive reconciliation query stale, so the dashboard re-reads it on return', async () => {
    // On the exchanges page the dashboard is not mounted: nothing observes the reconciliation.
    const exchanges = fakeExchanges({ exchanges: [exchange()] });
    server.use(...exchanges.handlers);
    const client = createQueryClient();
    client.setQueryData(reconciliationQueryKey, reconciliation({ exchanges: [] }));
    expect(client.getQueryState(reconciliationQueryKey)?.isInvalidated).toBe(false);
    const hook = renderHook(() => useSyncExchanges(), { wrapper: wrapperFor(client) });

    act(() => {
      hook.result.current.mutate();
    });
    await waitFor(() => {
      expect(hook.result.current.isSuccess).toBe(true);
    });

    await waitFor(() => {
      expect(client.getQueryState(reconciliationQueryKey)?.isInvalidated).toBe(true);
    });
  });
});

/**
 * Spec 024: the fills query is keyed `['exchanges', 'fills', filters, page]`, so the
 * invalidation a settled sync already makes reaches it, and it has no poll of its own.
 */
describe('useExchangeFills', () => {
  const MARCH: FillFilters = { exchanges: ['bitget'], fromDay: '2026-03-01', toDay: '2026-03-31' };

  function setUp(filters: FillFilters, page: number) {
    const rows = manyFills(60);
    const fake = fakeExchanges({ exchanges: [exchange({ fills_stored: 60 })], fills: rows });
    server.use(...fake.handlers);
    const client = createQueryClient();
    const hook = renderHook(
      ({ current, at }) => ({ fills: useExchangeFills(current, at), sync: useSyncExchanges() }),
      { wrapper: wrapperFor(client), initialProps: { current: filters, at: page } },
    );
    return { fake, client, hook };
  }

  it('is keyed under exchanges, with the filters and the page', async () => {
    const { client, hook } = setUp(MARCH, 2);
    await waitFor(() => {
      expect(hook.result.current.fills.isFetched).toBe(true);
    });

    expect(exchangeFillsQueryKey).toEqual(['exchanges', 'fills']);
    const queries = client.getQueryCache().findAll({ queryKey: ['exchanges', 'fills'] });
    expect(queries.map((query) => query.queryKey)).toEqual([['exchanges', 'fills', MARCH, 2]]);
  });

  it('asks for the page the filters and the page number name', async () => {
    inTimeZone('UTC');
    const { fake, hook } = setUp(MARCH, 2);
    await waitFor(() => {
      expect(hook.result.current.fills.isSuccess).toBe(true);
    });

    const [query] = fake.fillQueries();
    expect(query?.toString()).toBe(
      'exchange=bitget&from=2026-03-01T00%3A00%3A00.000Z&to=2026-04-01T00%3A00%3A00.000Z' +
        '&limit=5&offset=5',
    );
  });

  it('has no poll of its own', async () => {
    const { client, hook } = setUp(NO_FILTERS, 1);
    await waitFor(() => {
      expect(hook.result.current.fills.isSuccess).toBe(true);
    });

    const [query] = client.getQueryCache().findAll({ queryKey: ['exchanges', 'fills'] });
    expect(query?.options).not.toHaveProperty('refetchInterval');
  });

  it('is re-read when a sync settles', async () => {
    const { fake, hook } = setUp(NO_FILTERS, 1);
    await waitFor(() => {
      expect(hook.result.current.fills.isSuccess).toBe(true);
    });
    const before = fake.count('fills');

    act(() => {
      hook.result.current.sync.mutate();
    });

    await waitFor(() => {
      expect(fake.count('fills')).toBe(before + 1);
    });
  });

  it('sends nothing for a range whose end is before its start', async () => {
    const { fake, hook } = setUp({ ...NO_FILTERS, fromDay: '2026-03-31', toDay: '2026-03-01' }, 1);
    await settle();

    expect(fake.count('fills')).toBe(0);
    expect(hook.result.current.fills.fetchStatus).toBe('idle');
    expect(hook.result.current.fills.data).toBeUndefined();
  });

  it('keeps the last page while the next loads, and nothing across a change of filters', async () => {
    const { fake, hook } = setUp(NO_FILTERS, 1);
    await waitFor(() => {
      expect(hook.result.current.fills.isSuccess).toBe(true);
    });
    const first = hook.result.current.fills.data;

    const release = fake.hold('fills');
    hook.rerender({ current: NO_FILTERS, at: 2 });
    await waitFor(() => {
      expect(fake.count('fills')).toBe(2);
    });
    expect(hook.result.current.fills.isPlaceholderData).toBe(true);
    expect(hook.result.current.fills.data).toBe(first);

    // Equal filters in a new object are the same filters.
    hook.rerender({ current: { ...NO_FILTERS }, at: 3 });
    await waitFor(() => {
      expect(fake.count('fills')).toBe(3);
    });
    expect(hook.result.current.fills.data).toBe(first);

    hook.rerender({ current: { ...NO_FILTERS, exchanges: ['bitget'] }, at: 1 });
    await waitFor(() => {
      expect(fake.count('fills')).toBe(4);
    });
    expect(hook.result.current.fills.isPending).toBe(true);
    expect(hook.result.current.fills.data).toBeUndefined();
    release();
  });
});
