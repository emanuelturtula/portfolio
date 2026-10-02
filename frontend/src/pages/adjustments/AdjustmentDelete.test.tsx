import { act, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from 'vitest';

import { NOW } from '@/test/accountingFixtures';
import {
  adjustment,
  ADJUSTMENT_NOT_FOUND_DETAIL,
  BTC_OPENING,
  ETH_PRECISE,
  KAS_UNKNOWN_COST,
} from '@/test/adjustmentFixtures';
import {
  cell,
  EMPTY_FIELDS,
  exactly,
  field,
  fieldValues,
  fillForm,
  listRegion,
  loadedTable,
  openAdjustmentsPage,
  queryRowButton,
  queryTable,
  retype,
  rowButton,
  rowButtons,
  rowOf,
  shownAssets,
  startEditing,
  statusLine,
  submitButton,
  theForm,
  VALID_ENTRY,
} from '@/test/adjustmentsPage';
import { adjustmentPath } from '@/test/fakeAdjustments';
import { settle } from '@/test/render';
import { problem } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * Deleting an adjustment (spec 027, "The list"; acceptance criterion 12), rendered inside the
 * whole app at `/adjustments`: the confirmation, where focus goes at each step, the one
 * `DELETE`, the status line, a delete that fails, a delete of an adjustment that was already
 * gone (R13), and what a delete does to the form.
 *
 * Focus is asserted at every step because every step removes the control that was pressed: a
 * pressed button that vanishes without handing focus on leaves a keyboard user on `<body>`,
 * at the top of the page.
 *
 * **Time zones.** Only the test of the controls' full names depends on the zone, and it pins
 * it. Every other test finds a row's controls by the text they show.
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

function listHeading(): HTMLElement {
  return screen.getByRole('heading', { name: 'Recorded adjustments' });
}

/** The alerts inside a row: where a delete that failed says so. */
function rowAlerts(asset: string): string[] {
  return within(rowOf(asset))
    .queryAllByRole('alert')
    .map((alert) => alert.textContent);
}

/**
 * Every `role="alert"` that enters the document from the moment this is called, by its text,
 * including one that is there for a single render: reading the screen afterwards cannot see an
 * alert that came and went. The function returned gives what has been seen so far.
 */
function watchAlerts(): () => string[] {
  const seen: string[] = [];
  const note = (records: MutationRecord[]): void => {
    for (const record of records) {
      for (const node of Array.from(record.addedNodes)) {
        if (node instanceof HTMLElement) {
          const alerts = node.matches('[role="alert"]')
            ? [node]
            : Array.from(node.querySelectorAll('[role="alert"]'));
          seen.push(...alerts.map((alert) => alert.textContent));
        }
      }
    }
  };
  const observer = new MutationObserver(note);
  observer.observe(document.body, { childList: true, subtree: true });
  onTestFinished(() => {
    observer.disconnect();
  });
  return () => {
    note(observer.takeRecords());
    return seen;
  };
}

describe('deleting asks first (criterion 12)', () => {
  it('replaces Delete with "Confirm delete" and "Cancel", moves focus to the confirm button, and sends nothing', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);

    await user.click(rowButton(rowOf('KAS'), 'Delete'));

    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toHaveFocus();
    await settle();
    expect(adjustments.count('delete')).toBe(0);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(statusLine()).toBeNull();
    // Only the row that was asked about is asking.
    expect(rowButtons(rowOf('BTC'))).toEqual(['Edit', 'Delete']);
    expect(rowButtons(rowOf('ETH'))).toEqual(['Edit', 'Delete']);
  });

  it('names the confirmation for its row: the asset and the date', async () => {
    // Zone: pinned to UTC.
    inTimeZone('UTC');
    const { user } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));

    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toHaveAccessibleName(
      exactly('Confirm delete of KAS acquired Jun 1, 2025, 12:00 PM'),
    );
    expect(rowButton(rowOf('KAS'), 'Cancel')).toHaveAccessibleName(
      exactly('Cancel deleting KAS acquired Jun 1, 2025, 12:00 PM'),
    );
  });

  it('Cancel puts Delete back, returns focus to it, and sends nothing', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Cancel'));

    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);
    expect(rowButton(rowOf('KAS'), 'Delete')).toHaveFocus();
    await settle();
    expect(adjustments.count('delete')).toBe(0);
    expect(adjustments.adjustments()).toHaveLength(3);
    expect(statusLine()).toBeNull();
  });

  it('works from the keyboard: Enter asks, Enter again confirms', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    rowButton(rowOf('KAS'), 'Delete').focus();
    await user.keyboard('{Enter}');
    // One press asks. It does not delete.
    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toHaveFocus();
    await settle();
    expect(adjustments.count('delete')).toBe(0);

    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(shownAssets()).toEqual(['BTC', 'ETH']);
    });
    expect(adjustments.count('delete')).toBe(1);
  });

  it('lets two rows ask at once, and confirms only the one confirmed', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('ETH'), 'Delete'));
    await user.click(rowButton(rowOf('ETH'), 'Confirm delete'));

    await waitFor(() => {
      expect(shownAssets()).toEqual(['BTC', 'KAS']);
    });
    expect(
      adjustments.requestsTo('delete').map((request) => new URL(request.url).pathname),
    ).toEqual([adjustmentPath(3)]);
    // BTC is still asking: its question was not answered by ETH's.
    expect(rowButtons(rowOf('BTC'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
  });
});

describe('a confirmed delete (criterion 12)', () => {
  it('sends exactly one DELETE to the adjustment, declaring JSON and carrying no body', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });

    const requests = adjustments.requestsTo('delete');
    expect(requests).toHaveLength(1);
    expect(requests[0]?.method).toBe('DELETE');
    expect(new URL(requests[0]?.url ?? '').pathname).toBe(adjustmentPath(KAS_UNKNOWN_COST.id));
    // The backend's write guard refuses a write that does not declare JSON, body or no body.
    expect(requests[0]?.contentType).toBe('application/json');
    expect(requests[0]?.text).toBe('');
    expect(adjustments.count('create')).toBe(0);
    expect(adjustments.count('replace')).toBe(0);
  });

  it('removes the row, says "Adjustment deleted." and moves focus to the list\'s heading', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(screen.getByText('Adjustment deleted.')).toHaveAttribute('role', 'status');
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(adjustments.adjustments().map((entry) => entry.id)).toEqual([1, 3]);
    // The row is gone and its buttons with it: focus lands on the list, not on <body>.
    expect(listHeading()).toHaveFocus();
  });

  it('disables both buttons while the request is pending, and sends it once', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    });
    expect(rowButton(rowOf('KAS'), 'Cancel')).toBeDisabled();
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(rowButton(rowOf('KAS'), 'Cancel'));
    await settle();

    expect(adjustments.count('delete')).toBe(1);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
    expect(statusLine()).toBeNull();

    release();
    await waitFor(() => {
      expect(shownAssets()).toEqual(['BTC', 'ETH']);
    });
    expect(adjustments.count('delete')).toBe(1);
  });

  it('says nothing, and keeps the row waiting, until the list has been read again', async () => {
    // The DELETE has been answered; the read after it has not. Saying "deleted" over a row
    // that is still on screen, and taking focus from it, would be a line about something the
    // page does not show yet.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await settle();
    const listReads = adjustments.count('list');
    const release = adjustments.hold('list');

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(adjustments.count('list')).toBeGreaterThan(listReads);
    });
    await settle();

    // Deleted on the server, and the page has not read that yet.
    expect(adjustments.adjustments().map((entry) => entry.id)).toEqual([1, 3]);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    expect(rowButton(rowOf('KAS'), 'Cancel')).toBeDisabled();
    expect(statusLine()).toBeNull();
    expect(listHeading()).not.toHaveFocus();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(listHeading()).toHaveFocus();
    expect(adjustments.count('delete')).toBe(1);
  });

  it('shows "No adjustments yet" after the last one is deleted, with focus on the heading', async () => {
    const { user } = openAdjustmentsPage({ adjustments: [adjustment()] });
    await loadedTable();

    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));

    expect(await screen.findByRole('heading', { name: 'No adjustments yet' })).toBeVisible();
    expect(queryTable()).toBeNull();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(listHeading()).toHaveFocus();
  });

  it('replaces what the status line said before', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    await user.click(rowButton(rowOf('SOL'), 'Delete'));
    await user.click(rowButton(rowOf('SOL'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(screen.queryByText('Adjustment recorded.')).toBeNull();
  });
});

describe('the status line and a delete', () => {
  it('takes down what the line said when a delete is confirmed, before the answer is known', async () => {
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    // Asking does not clear it: nothing has been attempted yet.
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    expect(statusLine()).toBe('Adjustment recorded.');

    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    });
    expect(statusLine()).toBeNull();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
  });

  it('does not leave "Adjustment deleted." beside the error of the next delete, which failed', async () => {
    // One delete works and the next does not. The line about the first must not still be
    // saying "deleted" next to the alert that says the second was not.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });

    adjustments.fail('delete', () =>
      problem(500, 'Internal Server Error', 'The adjustment could not be deleted.'),
    );
    await user.click(rowButton(rowOf('ETH'), 'Delete'));
    await user.click(rowButton(rowOf('ETH'), 'Confirm delete'));

    await waitFor(() => {
      expect(rowAlerts('ETH')).toEqual(['The adjustment could not be deleted.']);
    });
    expect(statusLine()).toBeNull();
    expect(screen.queryByText('Adjustment deleted.')).toBeNull();
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
  });

  it('Cancel on the confirmation leaves the line as it was', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Cancel'));

    expect(statusLine()).toBe('Adjustment recorded.');
  });
});

describe('where focus goes after a delete', () => {
  it("takes focus to the list's heading when it had fallen to <body> while the delete was pending", async () => {
    // A browser may take focus off a button the moment it is disabled, and "Confirm delete"
    // is disabled while its request is out; or the owner clicks on the page's background
    // while they wait. Focus is then on <body>, which is nobody's, and the delete is still
    // what the owner is attending to. jsdom leaves focus on a disabled button and will not
    // blur one, so the test puts focus on <body> by way of the table's scroll region.
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    });
    act(() => {
      listRegion().focus();
      listRegion().blur();
    });
    expect(document.body).toHaveFocus();
    release();

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(listHeading()).toHaveFocus();
  });

  it('leaves focus where the owner has moved it while the delete was pending', async () => {
    // The row is gone when the answer lands, and the owner is typing in the form by then:
    // dragging focus to the list would take the keyboard out of their hands.
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(field('Note'));
    await user.type(field('Note'), 'Typing while it deletes');
    release();

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(field('Note')).toHaveFocus();
    expect(field('Note')).toHaveValue('Typing while it deletes');
  });

  it('when the list cannot be read again, takes the row out of its confirmation and moves focus to the heading', async () => {
    // The delete happened; the read after it failed, so the row is still on screen. It must
    // not be left asking to confirm something already done.
    const { user, adjustments } = openAdjustmentsPage({
      onChange: ({ adjustments: fake }) => {
        fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(adjustments.adjustments().map((entry) => entry.id)).toEqual([1, 3]);
    expect(
      screen.getByText(
        'Could not refresh the adjustments: The database is busy. Showing what was last loaded.',
      ),
    ).toHaveAttribute('role', 'alert');
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);
    expect(listHeading()).toHaveFocus();
  });

  it('when the list cannot be read again and the owner has moved on, leaves focus with them', async () => {
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
      onChange: ({ adjustments: fake }) => {
        fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(field('Asset'));
    release();

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);
    expect(field('Asset')).toHaveFocus();
  });
});

describe('a delete that fails (criterion 12)', () => {
  it("shows the API's detail in an alert beside the row, deletes nothing and reads nothing again", async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('delete', () =>
          problem(500, 'Internal Server Error', 'The adjustment could not be deleted.'),
        );
      },
    });
    await loadedTable();
    await settle();
    const listReads = adjustments.count('list');
    const alerts = watchAlerts();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(rowAlerts('KAS')).toEqual(['The adjustment could not be deleted.']);
    });
    await settle();
    // A 500 says nothing about whether the adjustment is there: the list is not stale because
    // of it. (And the control on `watchAlerts`: it sees an alert when there is one.)
    expect(adjustments.count('list')).toBe(listReads);
    expect(alerts()).toEqual(['The adjustment could not be deleted.']);
    expect(listHeading()).not.toHaveFocus();
    // Beside the row it is about, and no other.
    expect(cell(rowOf('KAS'), 'Actions')).toContainElement(
      screen.getByText('The adjustment could not be deleted.'),
    );
    expect(rowAlerts('BTC')).toEqual([]);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(adjustments.adjustments()).toHaveLength(3);
    expect(statusLine()).toBeNull();
    // Still asking, and able to be asked again.
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeEnabled();
    expect(rowButton(rowOf('KAS'), 'Cancel')).toBeEnabled();
  });

  it.each([
    ['a network failure', () => HttpResponse.error()],
    [
      "a proxy's HTML page",
      () =>
        new HttpResponse('<html><body>Bad Gateway</body></html>', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: { 'content-type': 'text/html' },
        }),
    ],
  ])(
    'falls back to its own sentence on %s, and the row stays as it was',
    async (_label, respond) => {
      const { user, adjustments } = openAdjustmentsPage({
        before: ({ adjustments: fake }) => {
          fake.fail('delete', respond);
        },
      });
      await loadedTable();
      await settle();
      const listReads = adjustments.count('list');

      await user.click(rowButton(rowOf('KAS'), 'Delete'));
      await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

      await waitFor(() => {
        expect(rowAlerts('KAS')).toEqual(['Could not delete the adjustment. Try again.']);
      });
      await settle();
      expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
      expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
      expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeEnabled();
      expect(statusLine()).toBeNull();
      expect(adjustments.count('list')).toBe(listReads);
      expect(adjustments.adjustments()).toHaveLength(3);
    },
  );

  it('deletes on a second confirmation once the server answers, and the alert goes', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('delete', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await loadedTable();
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowAlerts('KAS')).toEqual(['The database is busy.']);
    });

    adjustments.fail('delete', null);
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(adjustments.count('delete')).toBe(2);
    expect(screen.queryByText('The database is busy.')).toBeNull();
  });

  it('Cancel after a failure takes the alert down and returns focus to Delete', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('delete', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await loadedTable();
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowAlerts('KAS')).toEqual(['The database is busy.']);
    });

    await user.click(rowButton(rowOf('KAS'), 'Cancel'));

    // The owner has decided not to delete: the alert about the attempt goes with the decision.
    expect(rowAlerts('KAS')).toEqual([]);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);
    expect(rowButton(rowOf('KAS'), 'Delete')).toHaveFocus();
    expect(adjustments.count('delete')).toBe(1);
  });
});

describe('a delete of an adjustment that is already gone (criterion 12; spec 027, R13)', () => {
  const ALREADY_DELETED = 'That adjustment was already deleted.';

  it("says it was already deleted, takes the row away and moves focus to the list's heading", async () => {
    // Deleted in another tab, or from the console. The owner asked for it to be gone and it
    // is: they are told what happened, not left with a row that vanished in silence.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(screen.getByText(ALREADY_DELETED)).toHaveAttribute('role', 'status');
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(listHeading()).toHaveFocus();
    expect(document.body).not.toHaveFocus();
    expect(screen.queryByText('Adjustment deleted.')).toBeNull();
    expect(screen.queryAllByRole('alert')).toEqual([]);
    expect(
      adjustments.requestsTo('delete').map((request) => new URL(request.url).pathname),
    ).toEqual([adjustmentPath(KAS_UNKNOWN_COST.id)]);
  });

  it('never shows an alert: the row waits, disabled, until the list has been read again', async () => {
    // The fake answers at once, so a refetch that is awaited and one that is fired and
    // forgotten look the same unless the read is held open. Held, the difference shows: the
    // 404 has been answered, and the row must not say anything about it while it is still
    // there. `watchAlerts` sees an alert that lasts one render, too.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await settle();
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    const listReads = adjustments.count('list');
    const release = adjustments.hold('list');

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    const alerts = watchAlerts();
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(adjustments.count('list')).toBeGreaterThan(listReads);
    });
    await settle();

    expect(adjustments.count('delete')).toBe(1);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Confirm delete', 'Cancel']);
    expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    expect(rowButton(rowOf('KAS'), 'Cancel')).toBeDisabled();
    expect(screen.queryAllByRole('alert')).toEqual([]);
    expect(statusLine()).toBeNull();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    await settle();

    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(alerts()).toEqual([]);
    expect(screen.queryAllByRole('alert')).toEqual([]);
    expect(listHeading()).toHaveFocus();
    expect(adjustments.count('delete')).toBe(1);
  });

  it('is told apart from a delete that worked, one after the other', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });

    adjustments.deleteElsewhere(ETH_PRECISE.id);
    await user.click(rowButton(rowOf('ETH'), 'Delete'));
    await user.click(rowButton(rowOf('ETH'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(shownAssets()).toEqual(['BTC']);

    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(await screen.findByRole('heading', { name: 'No adjustments yet' })).toBeVisible();
    expect(adjustments.count('delete')).toBe(3);
  });

  it('shows the list as the server now has it, not only without that row', async () => {
    // The other session deleted two. The page asked to delete one of them, and what it shows
    // afterwards is what it read, not its old list with one row taken out.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    adjustments.deleteElsewhere(BTC_OPENING.id);

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(shownAssets()).toEqual(['ETH']);
    expect(listHeading()).toHaveFocus();
  });

  it('when its row has already left the list by the time it is answered, leaves focus alone and still says what happened', async () => {
    // The delete is out. Before it is answered, another session deletes the same adjustment
    // and the list is read again - a sync does that, and so does any change made here - so
    // the row that asked is no longer on screen, and its request is still pending. The answer
    // is a 404. There is no row left to hold focus or to show an alert, and the owner is
    // typing in the form: they are told, and nothing else moves.
    let release: () => void = () => undefined;
    const { user, adjustments, queryClient } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(field('Note'));
    await user.type(field('Note'), 'Typing');
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'adjustments'] }));
    await waitFor(() => {
      expect(shownAssets()).toEqual(['BTC', 'ETH']);
    });
    // The row has gone, and nothing has been said yet: the request is still out.
    expect(statusLine()).toBeNull();
    expect(adjustments.count('delete')).toBe(1);

    const alerts = watchAlerts();
    release();
    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    await settle();

    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(field('Note')).toHaveFocus();
    expect(field('Note')).toHaveValue('Typing');
    expect(alerts()).toEqual([]);
    expect(adjustments.count('delete')).toBe(1);
  });

  it('leaves focus with the owner when they have moved on to the form meanwhile', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });
    await loadedTable();
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(field('Note'));
    await user.type(field('Note'), 'Typing while it deletes');
    release();

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(shownAssets()).toEqual(['BTC', 'ETH']);
    expect(field('Note')).toHaveFocus();
    expect(field('Note')).toHaveValue('Typing while it deletes');
  });

  it('empties the form that was editing it, with focus going to the list when it was on the confirm button', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    adjustments.deleteElsewhere(BTC_OPENING.id);
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(shownAssets()).toEqual(['KAS', 'ETH']);
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(within(theForm()).queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(listHeading()).toHaveFocus();
    expect(screen.queryAllByRole('alert')).toEqual([]);
  });

  it("empties the form that was editing it, and the new form's heading takes focus when the owner was in the form", async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });

    await startEditing(user, 'BTC');
    adjustments.deleteElsewhere(BTC_OPENING.id);
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await user.click(field('Note'));
    expect(field('Note')).toHaveFocus();
    release();

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(shownAssets()).toEqual(['KAS', 'ETH']);
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(screen.getByRole('heading', { name: 'Record an adjustment' })).toHaveFocus();
    expect(document.body).not.toHaveFocus();
  });

  it('after that, the next save is a create: nothing is sent to the id that is gone', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    adjustments.deleteElsewhere(BTC_OPENING.id);
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('replace')).toBe(0);
    expect(adjustments.count('create')).toBe(1);
  });

  it('leaves an edit of another adjustment, and what was typed in it, alone', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Note', 'Half-written.');
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(fieldValues()).toMatchObject({ Asset: 'BTC', Note: 'Half-written.' });
  });

  it('replaces what the status line said before, which Confirm had already taken down', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    const release = adjustments.hold('delete');
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowButton(rowOf('KAS'), 'Confirm delete')).toBeDisabled();
    });
    expect(statusLine()).toBeNull();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    expect(screen.queryByText('Adjustment recorded.')).toBeNull();
  });

  it('when the list cannot be read again, still says so, and the row that is left says why it is not gone', async () => {
    // The accepted edge of R13, pinned as it is built and no further: the delete was answered
    // 404 and the read after it failed, so the row cannot be taken away. The status line says
    // what is known; the row leaves its confirmation and carries the API's sentence; the list
    // says it is stale. Nothing is left asking to confirm something already done.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await settle();
    adjustments.deleteElsewhere(KAS_UNKNOWN_COST.id);
    adjustments.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));

    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe(ALREADY_DELETED);
    });
    await waitFor(() => {
      expect(rowAlerts('KAS')).toEqual([ADJUSTMENT_NOT_FOUND_DETAIL]);
    });
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(rowButtons(rowOf('KAS'))).toEqual(['Edit', 'Delete']);
    expect(listHeading()).toHaveFocus();
    expect(
      screen.getByText(
        'Could not refresh the adjustments: The database is busy. Showing what was last loaded.',
      ),
    ).toHaveAttribute('role', 'alert');
  });
});

describe('a delete and the form (criterion 12)', () => {
  it('deleting the adjustment the form is editing returns the form to an empty create form', async () => {
    const { user } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['KAS', 'ETH']);
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(within(theForm()).queryByRole('button', { name: 'Cancel' })).toBeNull();
    // The delete was made from the list, and focus stays with the list.
    expect(listHeading()).toHaveFocus();
  });

  it('deleting it while the owner is in the form moves focus to the new form, not to <body>', async () => {
    // The owner confirmed the delete and went on typing in the edit form. The form is emptied
    // when the answer lands, which removes the field that had focus: the new form's heading
    // takes it, and the list does not pull it away.
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });

    await startEditing(user, 'BTC');
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await user.click(field('Note'));
    expect(field('Note')).toHaveFocus();
    release();

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(shownAssets()).toEqual(['KAS', 'ETH']);
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(screen.getByRole('heading', { name: 'Record an adjustment' })).toHaveFocus();
    expect(document.body).not.toHaveFocus();
  });

  it('deleting another one while the owner is in the form leaves focus, and the form, alone', async () => {
    let release: () => void = () => undefined;
    const { user } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('delete');
      },
    });

    await startEditing(user, 'BTC');
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));
    await user.click(field('Note'));
    await user.type(field('Note'), ' More.');
    release();

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(field('Note')).toHaveFocus();
    expect(field('Note')).toHaveValue(`${BTC_OPENING.note} More.`);
  });

  it('after that, the next save is a create: nothing is sent to the deleted id', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('replace')).toBe(0);
    expect(adjustments.count('create')).toBe(1);
  });

  it('deleting another adjustment leaves the edit, and what was typed in it, alone', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    await retype(user, 'Note', 'Half-written.');
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(fieldValues()).toMatchObject({ Asset: 'BTC', Quantity: '99', Note: 'Half-written.' });

    // And the edit still saves to the adjustment it was editing.
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(
      adjustments.requestsTo('replace').map((request) => new URL(request.url).pathname),
    ).toEqual([adjustmentPath(BTC_OPENING.id)]);
  });

  it('deleting a row leaves a half-filled create form alone', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { asset: 'SOL', quantity: '2.5', note: 'Half-written.' });
    await user.click(rowButton(rowOf('KAS'), 'Delete'));
    await user.click(rowButton(rowOf('KAS'), 'Confirm delete'));

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment deleted.');
    });
    expect(fieldValues()).toMatchObject({ Asset: 'SOL', Quantity: '2.5', Note: 'Half-written.' });
  });

  it('a delete that fails leaves the form editing what it was editing', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('delete', () => problem(503, 'Service Unavailable', 'Busy.'));
      },
    });

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    await user.click(rowButton(rowOf('BTC'), 'Delete'));
    await user.click(rowButton(rowOf('BTC'), 'Confirm delete'));
    await waitFor(() => {
      expect(rowAlerts('BTC')).toEqual(['Busy.']);
    });

    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(fieldValues()).toMatchObject({ Asset: 'BTC', Quantity: '99' });
    expect(queryRowButton(rowOf('BTC'), 'Confirm delete')).not.toBeNull();
  });
});
