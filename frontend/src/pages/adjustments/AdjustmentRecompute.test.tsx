import { useQuery } from '@tanstack/react-query';
import { act, screen, waitFor, within } from '@testing-library/react';
import type { UserEvent } from '@testing-library/user-event';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useReconciliation } from '@/api/accounting';
import {
  ALL_RECOMPUTE_OUTCOMES,
  emptySnapshot,
  failedFirstRecompute,
  failedRecompute,
  investedPortfolio,
  lastRecompute,
  noSnapshot,
  NOW,
  RECOMPUTE_ERROR,
  RECOMPUTE_FAILED_AT,
} from '@/test/accountingFixtures';
import {
  ASSET_SYMBOL_RULE,
  firstTrade,
  firstTrades,
  threeFirstTrades,
} from '@/test/adjustmentFixtures';
import {
  adjustmentsPage,
  field,
  fieldError,
  fieldValues,
  fillForm,
  formErrors,
  formReady,
  loadedTable,
  openAdjustmentsPage,
  precedes,
  recomputeAlert,
  retype,
  rowButton,
  rowOf,
  shownAssets,
  spaced,
  startEditing,
  statusLine,
  submitButton,
  theForm,
  VALID_ENTRY,
  EMPTY_FIELDS,
  type PageFakes,
} from '@/test/adjustmentsPage';
import { settle } from '@/test/render';
import { problem } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * What a change to an adjustment does to the rest of the app, and what the page says about the
 * recompute behind it (spec 027: "Files", "The page" item 2; acceptance criteria 13 and 14),
 * rendered inside the whole app at `/adjustments`.
 *
 * `fakeAdjustments` calls `onChange` after a write has changed its state and before it
 * answers, which is where the backend recomputes the positions. A test that replaces the
 * snapshot there and then sees it on the page has proved the page read it again after the
 * write - and nothing else can put it there, because no hook patches a cache.
 *
 * **Time zones.** The test of the alert's sentence pins UTC; the others hold in any zone.
 *
 * `Date` is faked and fixed at `NOW`; `setTimeout` stays real, because MSW answers through it.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

/** A snapshot that is still served, beside a recompute that failed after it was written. */
function staleSnapshot() {
  return emptySnapshot({ last_recompute: failedRecompute() });
}

/**
 * Two queries of the test's own, mounted beside the page: the holdings check's read, which
 * the adjustments page does not mount itself, and one under a key no hook of the page knows.
 * The second is what tells "invalidates the `['accounting']` root" from "invalidates the keys
 * it could think of".
 */
function Probes({ onRead }: { readonly onRead: () => void }) {
  useReconciliation();
  useQuery({
    queryKey: ['accounting', 'a-key-the-page-has-never-heard-of'],
    queryFn: () => {
      onRead();
      return 'read';
    },
  });
  return null;
}

type Change = (user: UserEvent) => Promise<void>;

const record: Change = async (user) => {
  await fillForm(user, VALID_ENTRY);
  await user.click(submitButton());
};

const edit: Change = async (user) => {
  await startEditing(user, 'BTC');
  await retype(user, 'Quantity', '1.75');
  await user.click(submitButton());
};

const remove: Change = async (user) => {
  await user.click(rowButton(rowOf('KAS'), 'Delete'));
  await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
};

const CHANGES: readonly (readonly [string, Change, string])[] = [
  ['recording', record, 'Adjustment recorded.'],
  ['editing', edit, 'Adjustment updated.'],
  ['deleting', remove, 'Adjustment deleted.'],
];

describe('every change invalidates the accounting root (criterion 13)', () => {
  it.each(CHANGES)(
    '%s reads again the positions, the holdings check, the list, the first trades, and a query under the root it does not know',
    async (_label, change, said) => {
      let probeReads = 0;
      const { user, accounting, adjustments } = openAdjustmentsPage({
        extra: (
          <Probes
            onRead={() => {
              probeReads += 1;
            }}
          />
        ),
      });
      await loadedTable();
      await settle();
      const before = {
        positions: accounting.count('positions'),
        reconciliation: accounting.count('reconciliation'),
        list: adjustments.count('list'),
        firstTrades: adjustments.count('first-trades'),
        probe: probeReads,
      };
      // The controls: each has been read, and nothing is reading on its own.
      expect(before.positions).toBeGreaterThan(0);
      expect(before.reconciliation).toBeGreaterThan(0);
      expect(before.probe).toBeGreaterThan(0);
      await settle();
      expect(accounting.count('positions')).toBe(before.positions);
      expect(probeReads).toBe(before.probe);

      await change(user);
      await waitFor(() => {
        expect(statusLine()).toBe(said);
      });
      await settle();

      expect(accounting.count('positions')).toBeGreaterThan(before.positions);
      expect(accounting.count('reconciliation')).toBeGreaterThan(before.reconciliation);
      expect(adjustments.count('list')).toBeGreaterThan(before.list);
      expect(adjustments.count('first-trades')).toBeGreaterThan(before.firstTrades);
      expect(probeReads).toBeGreaterThan(before.probe);
    },
  );

  it.each(CHANGES)(
    '%s puts on the page what the recompute behind it left: a mounted positions query refetches',
    async (_label, change, said) => {
      const { user } = openAdjustmentsPage({
        onChange: ({ accounting }) => {
          accounting.setPositions(staleSnapshot());
        },
      });
      await loadedTable();
      await settle();
      expect(recomputeAlert()).toBeNull();

      await change(user);

      await waitFor(() => {
        expect(recomputeAlert()).not.toBeNull();
      });
      await waitFor(() => {
        expect(statusLine()).toBe(said);
      });
    },
  );

  it('reads the first trades again, so the form offers what the server now says', async () => {
    const { user } = openAdjustmentsPage({
      onChange: ({ adjustments }) => {
        adjustments.setFirstTrades(
          firstTrades([...threeFirstTrades().assets, firstTrade('SOL', '2025-04-01T00:00:00Z')]),
        );
      },
    });
    await loadedTable();
    const offered = () =>
      Array.from(document.querySelectorAll('datalist option')).map((option) =>
        option.getAttribute('value'),
      );
    await waitFor(() => {
      expect(offered()).toEqual(['BTC', 'ETH', 'KAS']);
    });

    await record(user);

    await waitFor(() => {
      expect(offered()).toEqual(['BTC', 'ETH', 'KAS', 'SOL']);
    });
  });

  it('a change the server refuses invalidates nothing', async () => {
    let probeReads = 0;
    const { user, accounting, adjustments } = openAdjustmentsPage({
      extra: (
        <Probes
          onRead={() => {
            probeReads += 1;
          }}
        />
      ),
    });
    await loadedTable();
    await settle();
    const before = {
      positions: accounting.count('positions'),
      list: adjustments.count('list'),
      probe: probeReads,
    };

    await fillForm(user, { ...VALID_ENTRY, asset: 'sol' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(fieldError('Asset')).toBe(ASSET_SYMBOL_RULE);
    });
    await settle();

    expect(accounting.count('positions')).toBe(before.positions);
    expect(adjustments.count('list')).toBe(before.list);
    expect(probeReads).toBe(before.probe);
  });

  it('a delete answered 404 reads the whole root again: what deleted it moved the positions too', async () => {
    // Spec 027, R13. The adjustment was deleted elsewhere, and the recompute behind that
    // delete is as invisible to this page as the delete was. A 404 is the first the page
    // hears of either, so everything under the root is stale, not the list alone.
    let probeReads = 0;
    const { user, accounting, adjustments } = openAdjustmentsPage({
      extra: (
        <Probes
          onRead={() => {
            probeReads += 1;
          }}
        />
      ),
    });
    await loadedTable();
    await settle();
    const before = {
      positions: accounting.count('positions'),
      reconciliation: accounting.count('reconciliation'),
      list: adjustments.count('list'),
      firstTrades: adjustments.count('first-trades'),
      probe: probeReads,
    };
    expect(recomputeAlert()).toBeNull();

    // What the other session's delete left on the server.
    adjustments.deleteElsewhere(2);
    accounting.setPositions(staleSnapshot());
    await remove(user);
    await waitFor(() => {
      expect(statusLine()).toBe('That adjustment was already deleted.');
    });
    await settle();

    expect(accounting.count('positions')).toBeGreaterThan(before.positions);
    expect(accounting.count('reconciliation')).toBeGreaterThan(before.reconciliation);
    expect(adjustments.count('list')).toBeGreaterThan(before.list);
    expect(adjustments.count('first-trades')).toBeGreaterThan(before.firstTrades);
    expect(probeReads).toBeGreaterThan(before.probe);
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    // And what the page read is on it: the recompute that failed behind the other delete.
    expect(recomputeAlert()).not.toBeNull();
  });

  it.each([
    [
      'a 503',
      () => problem(503, 'Service Unavailable', 'The database is busy.'),
      'The database is busy.',
    ],
    [
      'a network failure',
      () => HttpResponse.error(),
      'Could not delete the adjustment. Try again.',
    ],
  ])('a delete that fails on %s invalidates nothing', async (_label, respond, said) => {
    let probeReads = 0;
    const { user, accounting, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('delete', respond);
      },
      extra: (
        <Probes
          onRead={() => {
            probeReads += 1;
          }}
        />
      ),
    });
    await loadedTable();
    await settle();
    const before = {
      positions: accounting.count('positions'),
      list: adjustments.count('list'),
      probe: probeReads,
    };

    await remove(user);
    await screen.findByText(said);
    await settle();

    expect(accounting.count('positions')).toBe(before.positions);
    expect(adjustments.count('list')).toBe(before.list);
    expect(probeReads).toBe(before.probe);
  });

  it('a save answered 404 reads the list again, and only the list', async () => {
    let probeReads = 0;
    const { user, accounting, adjustments } = openAdjustmentsPage({
      extra: (
        <Probes
          onRead={() => {
            probeReads += 1;
          }}
        />
      ),
    });
    await startEditing(user, 'BTC');
    await settle();
    const before = { positions: accounting.count('positions'), probe: probeReads };

    adjustments.deleteElsewhere(1);
    await user.click(submitButton());
    await waitFor(() => {
      expect(shownAssets()).toEqual(['KAS', 'ETH']);
    });
    await settle();

    // Nothing was changed by this page, so nothing else is stale because of it.
    expect(accounting.count('positions')).toBe(before.positions);
    expect(probeReads).toBe(before.probe);
  });
});

describe('a save is a save whatever the reads after it do', () => {
  it.each([
    [
      'the positions',
      (fakes: PageFakes) => {
        fakes.accounting.fail(() => problem(500, 'Internal Server Error', 'No snapshot.'));
      },
    ],
    [
      'the first trades',
      (fakes: PageFakes) => {
        fakes.adjustments.fail('first-trades', () =>
          problem(500, 'Internal Server Error', 'No fills.'),
        );
      },
    ],
    [
      'the list',
      (fakes: PageFakes) => {
        fakes.adjustments.fail('list', () => problem(500, 'Internal Server Error', 'No list.'));
      },
    ],
  ])(
    'says "Adjustment recorded." and empties the form when the read of %s then answers 500',
    async (_label, breakRead) => {
      const { user, adjustments } = openAdjustmentsPage({ onChange: breakRead });
      await loadedTable();

      await record(user);

      await waitFor(() => {
        expect(statusLine()).toBe('Adjustment recorded.');
      });
      expect(adjustments.adjustments().map((entry) => entry.asset)).toContain('SOL');
      expect(theForm()).toHaveAccessibleName('Record an adjustment');
      expect(fieldValues()).toEqual(EMPTY_FIELDS);
      expect(formErrors()).toEqual([]);
      expect(submitButton()).toBeEnabled();
    },
  );
});

describe('a failed last recompute is shown (criterion 14)', () => {
  it('says the last recompute failed, when, and with which error, and that changes are saved', async () => {
    // Zone: pinned to UTC, for the instant as it is read.
    inTimeZone('UTC');
    openAdjustmentsPage({ positions: staleSnapshot() });

    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });
    const alert = recomputeAlert();

    expect(spaced(alert?.textContent ?? null)).toBe(
      'The last recompute of the positions failed on Sep 24, 2026, 11:50 AM ' +
        `(${RECOMPUTE_ERROR}). Changes made here are saved, but the dashboard's figures are ` +
        'from before it.',
    );
    expect(alert).toHaveAttribute('role', 'alert');
    expect(alert?.tagName).toBe('P');
    // An absolute instant: a phrase that ticks inside a live region is re-announced each time.
    expect(alert?.querySelector('time')).toHaveAttribute('datetime', RECOMPUTE_FAILED_AT);
    expect(alert?.textContent).not.toMatch(/ago|just now/u);
  });

  it('sits between the introduction and the form', async () => {
    openAdjustmentsPage({ positions: staleSnapshot() });
    const page = await adjustmentsPage();
    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });
    const alert = recomputeAlert();
    const introduction = within(page).getByText(/^Coins the imported history does not show/u);

    expect(alert !== null && precedes(introduction, alert)).toBe(true);
    expect(alert !== null && precedes(alert, theForm())).toBe(true);
    expect(page).toContainElement(alert);
  });

  it.each([
    ['with no snapshot at all: the only recompute failed', failedFirstRecompute()],
    ['beside a snapshot with positions', investedPortfolio({ last_recompute: failedRecompute() })],
  ])('is shown %s', async (_label, positions) => {
    openAdjustmentsPage({ positions });
    await formReady();

    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });
  });

  it.each(ALL_RECOMPUTE_OUTCOMES.filter((outcome) => outcome !== 'failed'))(
    'shows nothing when the last recompute was "%s"',
    async (outcome) => {
      const { accounting } = openAdjustmentsPage({
        positions: emptySnapshot({ last_recompute: lastRecompute({ outcome }) }),
      });
      await loadedTable();
      await waitFor(() => {
        expect(accounting.count('positions')).toBeGreaterThan(0);
      });
      await settle();

      expect(recomputeAlert()).toBeNull();
      expect(screen.queryByRole('alert')).toBeNull();
    },
  );

  it('shows nothing when no recompute has been attempted', async () => {
    const { accounting } = openAdjustmentsPage({ positions: noSnapshot() });
    await loadedTable();
    await waitFor(() => {
      expect(accounting.count('positions')).toBeGreaterThan(0);
    });
    await settle();

    expect(recomputeAlert()).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows nothing while the positions are pending, and the page works', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      positions: staleSnapshot(),
      before: ({ accounting }) => {
        release = accounting.hold('positions');
      },
    });
    await loadedTable();
    await settle();

    expect(recomputeAlert()).toBeNull();
    // Neither a skeleton nor an error stands in for it: the page's own job does not depend
    // on this read.
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryAllByText(/Loading/u)).toEqual([]);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);

    // The form is filled and sent while the read hangs.
    await record(user);
    await waitFor(() => {
      expect(adjustments.adjustments().map((entry) => entry.asset)).toContain('SOL');
    });

    release();
    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
  });

  it.each([
    ['answers 500', () => problem(500, 'Internal Server Error', 'The snapshot could not be read.')],
    ['cannot be reached', () => HttpResponse.error()],
    [
      'answers something that is not JSON',
      () => new HttpResponse('<html></html>', { headers: { 'content-type': 'text/html' } }),
    ],
  ])('shows nothing when the positions read %s, and the page works', async (_label, respond) => {
    const { user, accounting } = openAdjustmentsPage({
      before: ({ accounting: fake }) => {
        fake.fail(respond);
      },
    });
    await loadedTable();
    await waitFor(() => {
      expect(accounting.count('positions')).toBeGreaterThan(0);
    });
    await settle();

    // A failed read of a warning is not a warning, and it is not reported as one either.
    expect(recomputeAlert()).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('The snapshot could not be read.')).toBeNull();
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);

    await record(user);
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(shownAssets()).toEqual(['SOL', 'BTC', 'KAS', 'ETH']);
  });

  it('keeps saying so when a later read of the positions fails', async () => {
    // What the last good read said is the latest the page knows about the recompute.
    const { accounting, queryClient } = openAdjustmentsPage({ positions: staleSnapshot() });
    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });
    const before = accounting.count('positions');

    accounting.fail(() => problem(503, 'Service Unavailable', 'Busy.'));
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'positions'] }));
    await waitFor(() => {
      expect(accounting.count('positions')).toBeGreaterThan(before);
    });
    await settle();

    expect(recomputeAlert()).not.toBeNull();
  });

  it('goes once a change is followed by a recompute that worked', async () => {
    const { user } = openAdjustmentsPage({
      positions: staleSnapshot(),
      onChange: ({ accounting }) => {
        accounting.setPositions(emptySnapshot());
      },
    });
    await loadedTable();
    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });

    await record(user);

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(recomputeAlert()).toBeNull();
  });

  it('does not stop the owner: the form under the alert records as usual', async () => {
    const { user, adjustments } = openAdjustmentsPage({ positions: staleSnapshot() });
    await loadedTable();
    await waitFor(() => {
      expect(recomputeAlert()).not.toBeNull();
    });

    await record(user);

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
    expect(field('Asset')).toHaveValue('');
    // Still failed, still said: the save did not make the recompute succeed.
    expect(recomputeAlert()).not.toBeNull();
  });
});
