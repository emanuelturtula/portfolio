import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NOW, ZERO } from '@/test/accountingFixtures';
import {
  adjustment,
  ADJUSTMENT_CREATED_AT,
  ADJUSTMENT_NOT_FOUND_DETAIL,
  ASSET_SYMBOL_RULE,
  BTC_OPENING,
  BTC_OPENING_AT,
  CASH_ASSET_RULE,
  ETH_ACQUIRED_AT,
  ETH_PRECISE,
  ethPrecise,
  KAS_ACQUIRED_AT,
  KAS_UNKNOWN_COST,
  kasUnknownCost,
  NOTE_TOO_LONG_RULE,
  OCCURRED_IN_FUTURE_RULE,
  QUANTITY_NOT_POSITIVE_RULE,
  QUANTITY_TOO_LARGE_RULE,
  QUANTITY_TOO_PRECISE_RULE,
  UNIT_COST_NEGATIVE_RULE,
  VALIDATION_DETAIL,
} from '@/test/adjustmentFixtures';
import {
  adjustmentsPage,
  cell,
  COLUMNS,
  dataValues,
  EMPTY_FIELDS,
  exactly,
  field,
  fieldError,
  fieldErrors,
  fieldValues,
  FIELD_LABELS,
  fillForm,
  formErrors,
  formReady,
  listRegion,
  loadedTable,
  NO_FIELD_ERRORS,
  openAdjustmentsPage,
  precedes,
  queryTable,
  retype,
  rowButton,
  rowOf,
  setDate,
  shownAssets,
  spaced,
  startEditing,
  statusElements,
  statusLine,
  submitButton,
  theForm,
  VALID_ENTRY,
  type FieldLabel,
} from '@/test/adjustmentsPage';
import {
  ADJUSTMENTS_PATH,
  adjustmentPath,
  adjustmentValidationProblem,
  refusal,
  type RecordedAdjustmentRequest,
} from '@/test/fakeAdjustments';
import { currentPath, settle } from '@/test/render';
import { problem } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The manual adjustments page (spec 027), rendered inside the whole app at `/adjustments`:
 * the page's order, the list and its states, recording, editing, what the form refuses and
 * what the server refuses, and the asset the URL carries. The suggested date, deleting, and
 * what a change does to the rest of the app each have a file of their own under
 * `pages/adjustments/`.
 *
 * `fakeAdjustments` holds every request to the request models and to the service's rules, in
 * the backend's order and with its sentences, and stores what the backend would store. So a
 * body asserted on here is one the backend was sent, and a refusal on screen is one it writes.
 *
 * `Date` is faked and fixed at `NOW`; `setTimeout` stays real, because MSW answers through it.
 *
 * **Time zones.** A test whose expectation depends on the zone names it with `inTimeZone`
 * before the page is opened. Every other test holds in any zone: its dates are in 2025, which
 * is before `NOW` wherever the clock is, and it asserts on nothing a zone moves.
 */
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date(NOW));
});

afterEach(() => {
  vi.useRealTimers();
});

/** The one request of `requests`. A second one is the failure, not something to index past. */
function only(requests: readonly RecordedAdjustmentRequest[]): RecordedAdjustmentRequest {
  const [request] = requests;
  if (request === undefined || requests.length !== 1) {
    throw new Error(`Expected one request, and there were ${String(requests.length)}.`);
  }
  return request;
}

/** {@link VALID_ENTRY} without its asset, for a form whose asset came from the URL. */
const ENTRY_WITHOUT_ASSET = {
  quantity: VALID_ENTRY.quantity,
  unitCost: VALID_ENTRY.unitCost,
  occurredAt: VALID_ENTRY.occurredAt,
  note: VALID_ENTRY.note,
};

const INTRODUCTION =
  'An adjustment records coins the imported history does not show, such as an opening balance ' +
  "bought before an exchange's history begins, or coins acquired off an exchange. See " +
  '"Recording what the history does not show" in docs/accounting.md.';

describe('the adjustments page', () => {
  it('is headed "Adjustments" and says what an adjustment is, naming the documentation', async () => {
    openAdjustmentsPage();

    const page = await adjustmentsPage();

    expect(within(page).getByRole('heading', { level: 2, name: 'Adjustments' })).toBeVisible();
    expect(within(page).getByText(INTRODUCTION).tagName).toBe('P');
    // In plain text: the documentation is a file in the repository, not a page to link to.
    await loadedTable();
    expect(within(page).queryByRole('link')).toBeNull();
  });

  it('puts the introduction first, then the form, then the list', async () => {
    openAdjustmentsPage();

    const page = await adjustmentsPage();
    await loadedTable();
    const heading = within(page).getByRole('heading', { level: 2, name: 'Adjustments' });
    const introduction = within(page).getByText(INTRODUCTION);
    const form = within(page).getByRole('form', { name: 'Record an adjustment' });
    const listHeading = within(page).getByRole('heading', {
      level: 3,
      name: 'Recorded adjustments',
    });

    expect(precedes(heading, introduction)).toBe(true);
    expect(precedes(introduction, form)).toBe(true);
    expect(precedes(form, listHeading)).toBe(true);
    expect(precedes(listHeading, listRegion())).toBe(true);
    // The form is headed at the list's level, under the page's own heading.
    expect(within(form).getByRole('heading', { level: 3 })).toHaveTextContent(
      'Record an adjustment',
    );
  });

  it('opens on an empty create form, with nothing focused and nothing said', async () => {
    openAdjustmentsPage();

    const form = await formReady();
    await loadedTable();

    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(formErrors()).toEqual([]);
    expect(statusLine()).toBeNull();
    expect(
      within(form)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual(['Record adjustment']);
    expect(submitButton()).toBeEnabled();
    // Arriving on the page does not move focus into it.
    expect(document.body).toHaveFocus();
  });

  it('gives the five fields the controls the spec names', async () => {
    openAdjustmentsPage();
    await formReady();

    const asset = field('Asset');
    expect(asset).toHaveAttribute('type', 'text');
    expect(asset).toHaveAttribute('autocapitalize', 'characters');
    expect(asset).toHaveAttribute('autocomplete', 'off');
    expect(asset).toHaveAttribute('spellcheck', 'false');
    expect(asset).toHaveAccessibleDescription(
      'The symbol exactly as the exchanges spell it, in upper case, such as BTC.',
    );

    // Amounts are typed as text: a `type="number"` input would hand back a float. And with no
    // `inputmode`: a decimal keypad follows the phone's locale, and where that is a comma the
    // owner could not type the dot the server reads (spec 027, R9).
    for (const label of ['Quantity', 'Unit cost (USD)'] as const) {
      expect(field(label)).toHaveAttribute('type', 'text');
      expect(field(label)).not.toHaveAttribute('inputmode');
      expect(field(label)).toHaveAttribute('autocomplete', 'off');
    }
    expect(field('Quantity')).toHaveAccessibleDescription(
      'Use a dot for the decimals, such as 0.5.',
    );
    expect(field('Unit cost (USD)')).toHaveAccessibleDescription(
      'Use a dot for the decimals. Leave empty if unknown. An unknown cost is not zero: the ' +
        'units count toward the quantity held and are left out of the average cost.',
    );

    expect(field('Acquired on')).toHaveAttribute('type', 'datetime-local');
    expect(field('Note').tagName).toBe('TEXTAREA');
    expect(field('Note')).toHaveAccessibleDescription('Why you are recording this, in your words.');
  });

  it('marks the four required fields as required, and the unit cost as not', async () => {
    openAdjustmentsPage();
    await formReady();

    for (const label of ['Asset', 'Quantity', 'Acquired on', 'Note'] as const) {
      expect(field(label)).toHaveAttribute('aria-required', 'true');
    }
    expect(field('Unit cost (USD)')).not.toHaveAttribute('aria-required');
    // Said to assistive technology, and not enforced by the browser: a native `required`
    // would put the browser's own bubble where the form's message under the field belongs.
    for (const label of FIELD_LABELS) {
      expect(field(label)).not.toHaveAttribute('required');
    }
    expect(theForm()).toHaveAttribute('novalidate');
  });

  it('says which zone the date is in, by its name', async () => {
    // Zone: pinned. The hint names the zone the field is read in.
    inTimeZone('Europe/Madrid');
    openAdjustmentsPage();
    await formReady();

    expect(field('Acquired on')).toHaveAccessibleDescription('In your local time (Europe/Madrid).');
  });
});

describe('the list (criterion 6)', () => {
  it('has the six columns, in a keyboard-reachable region named by its heading', async () => {
    openAdjustmentsPage();

    const table = await loadedTable();
    const region = listRegion();

    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual(COLUMNS);
    // A phone scrolls the table and never the page: the region is what scrolls, and a
    // keyboard has to be able to reach what scrolls.
    expect(region).toHaveClass('table-scroll');
    expect(region).toHaveAttribute('tabindex', '0');
    expect(region).toContainElement(table);
    expect(screen.getByRole('heading', { level: 3, name: 'Recorded adjustments' })).toBeVisible();
  });

  it('shows each adjustment: asset, quantity, unit cost, date and note', async () => {
    // Zone: pinned to UTC, for the dates as they are read.
    inTimeZone('UTC');
    openAdjustmentsPage();
    await loadedTable();

    const shown = ['BTC', 'KAS', 'ETH'].map((asset) => {
      const row = rowOf(asset);
      return {
        asset: within(row).getByRole('rowheader').textContent,
        quantity: cell(row, 'Quantity').textContent,
        unitCost: cell(row, 'Unit cost (USD)').textContent,
        acquired: spaced(cell(row, 'Acquired').textContent),
        note: cell(row, 'Note').textContent,
      };
    });

    expect(shown).toEqual([
      {
        asset: 'BTC',
        quantity: '1.5',
        unitCost: '20,000.00',
        acquired: 'Feb 28, 2025, 11:59 PM',
        note: BTC_OPENING.note,
      },
      {
        asset: 'KAS',
        quantity: '12,000',
        unitCost: 'Unknown cost',
        acquired: 'Jun 1, 2025, 12:00 PM',
        note: KAS_UNKNOWN_COST.note,
      },
      {
        asset: 'ETH',
        quantity: '3.14159265',
        unitCost: '1,234.56789012',
        acquired: 'Aug 15, 2025, 8:30 AM',
        note: ETH_PRECISE.note,
      },
    ]);
  });

  it('carries every amount as the exact string the server sent, eighteen places and all', async () => {
    openAdjustmentsPage();
    await loadedTable();

    // As doubles these two are 3.141592653589793 and 1234.5678901234568.
    expect(dataValues(cell(rowOf('ETH'), 'Quantity'))).toEqual(['3.141592653589793238']);
    expect(dataValues(cell(rowOf('ETH'), 'Unit cost (USD)'))).toEqual(['1234.567890123456789012']);
    expect(dataValues(cell(rowOf('BTC'), 'Quantity'))).toEqual(['1.500000000000000000']);
    expect(dataValues(cell(rowOf('BTC'), 'Unit cost (USD)'))).toEqual(['20000.000000000000000000']);
    expect(dataValues(cell(rowOf('KAS'), 'Quantity'))).toEqual(['12000.000000000000000000']);
  });

  it('carries the largest and the smallest amounts there are without losing a digit', async () => {
    openAdjustmentsPage({
      adjustments: [
        adjustment({
          id: 1,
          asset: 'SHIB',
          quantity: '99999999999999999999.999999999999999999',
          unit_cost: null,
        }),
        adjustment({
          id: 2,
          asset: 'WBTC',
          quantity: '0.000000000000000001',
          unit_cost: '0.000000000000000001',
        }),
      ],
    });
    await loadedTable();

    expect(dataValues(cell(rowOf('SHIB'), 'Quantity'))).toEqual([
      '99999999999999999999.999999999999999999',
    ]);
    expect(dataValues(cell(rowOf('WBTC'), 'Quantity'))).toEqual(['0.000000000000000001']);
    expect(dataValues(cell(rowOf('WBTC'), 'Unit cost (USD)'))).toEqual(['0.000000000000000001']);
    // Too small for the eight places shown, and never shown as nothing.
    expect(cell(rowOf('WBTC'), 'Quantity')).toHaveTextContent('< 0.00000001');
    expect(cell(rowOf('WBTC'), 'Unit cost (USD)')).toHaveTextContent('< 0.00000001');
  });

  it('reads "Unknown cost" for a null cost: never a zero, never a dash', async () => {
    openAdjustmentsPage();
    await loadedTable();

    const unitCost = cell(rowOf('KAS'), 'Unit cost (USD)');

    expect(unitCost.textContent).toBe('Unknown cost');
    // No figure at all: a `<data>` here would be a number where there is none.
    expect(dataValues(unitCost)).toEqual([]);
    expect(unitCost.textContent).not.toMatch(/\d|—|-/u);
  });

  it('tells a cost of nothing from an unknown cost', async () => {
    openAdjustmentsPage({
      adjustments: [
        adjustment({ id: 1, asset: 'ARB', unit_cost: ZERO, note: 'An airdrop: it cost nothing.' }),
        kasUnknownCost(),
      ],
    });
    await loadedTable();

    const free = cell(rowOf('ARB'), 'Unit cost (USD)');
    const unknown = cell(rowOf('KAS'), 'Unit cost (USD)');

    expect(free.textContent).toBe('0.00');
    expect(dataValues(free)).toEqual([ZERO]);
    expect(unknown.textContent).toBe('Unknown cost');
    expect(free.textContent).not.toBe(unknown.textContent);
  });

  it('shows the date as an instant: a <time> holding the stored one, microseconds included', async () => {
    // Zone: pinned, east of UTC, where the stored instant is on the next local day.
    inTimeZone('Europe/Madrid');
    openAdjustmentsPage();
    await loadedTable();

    const times = ['BTC', 'KAS', 'ETH'].map((asset) => {
      const time = cell(rowOf(asset), 'Acquired').querySelector('time');
      return [time?.getAttribute('datetime'), spaced(time?.textContent ?? null)];
    });

    expect(times).toEqual([
      [BTC_OPENING_AT, 'Mar 1, 2025, 12:59 AM'],
      [KAS_ACQUIRED_AT, 'Jun 1, 2025, 2:00 PM'],
      [ETH_ACQUIRED_AT, 'Aug 15, 2025, 10:30 AM'],
    ]);
  });

  it("keeps the endpoint's order, which is neither by id nor by asset nor by the date's text", async () => {
    // Replay order is by `occurred_at`, then id: 5 and 7 share an instant, and 3 is a
    // fraction of a second after it. Sorted by id it would be XRP first; by asset, ADA; and by
    // the instants compared as text, XRP again, since "." sorts before "Z".
    const { adjustments } = openAdjustmentsPage({
      adjustments: [
        adjustment({ id: 3, asset: 'XRP', occurred_at: '2025-02-28T23:59:37.123456Z' }),
        adjustment({ id: 5, asset: 'SOL', occurred_at: '2025-02-28T23:59:37Z' }),
        adjustment({ id: 7, asset: 'ADA', occurred_at: '2025-02-28T23:59:37Z' }),
      ],
    });
    await loadedTable();

    expect(adjustments.adjustments().map((entry) => entry.id)).toEqual([5, 7, 3]);
    expect(shownAssets()).toEqual(['SOL', 'ADA', 'XRP']);
  });

  it('shows two adjustments of one asset as two rows', async () => {
    openAdjustmentsPage({
      adjustments: [
        adjustment({ id: 1 }),
        adjustment({ id: 2, quantity: '0.250000000000000000', occurred_at: KAS_ACQUIRED_AT }),
      ],
    });
    await loadedTable();

    expect(shownAssets()).toEqual(['BTC', 'BTC']);
    expect(
      within(listRegion())
        .getAllByRole('row')
        .slice(1)
        .map((row) => dataValues(cell(row, 'Quantity'))[0]),
    ).toEqual(['1.500000000000000000', '0.250000000000000000']);
  });

  it('shows a long note in full, in the cell the stylesheet wraps', async () => {
    // Whether it wraps is the stylesheet's, and jsdom lays nothing out: this pins the text
    // and the hook the rule hangs on. The 375 px check is made in a browser (criterion 17).
    const unbroken = 'x'.repeat(500);
    openAdjustmentsPage({ adjustments: [adjustment({ note: unbroken })] });
    await loadedTable();

    const note = cell(rowOf('BTC'), 'Note');

    expect(note.textContent).toBe(unbroken);
    expect(note).toHaveClass('note');
    expect(note.closest('table')).toHaveClass('adjustment-table');
  });

  it('shows a note as text: markup in it is not rendered', async () => {
    const note = '<b>Bought</b> at <a href="https://example.invalid">a meetup</a> & paid cash';
    openAdjustmentsPage({ adjustments: [adjustment({ note })] });
    await loadedTable();

    const shown = cell(rowOf('BTC'), 'Note');

    expect(shown.textContent).toBe(note);
    expect(shown.children).toHaveLength(0);
  });

  it('names each row control for its row: the asset and the date', async () => {
    // Zone: pinned to UTC. Every row's "Edit" would otherwise share one name.
    inTimeZone('UTC');
    openAdjustmentsPage();
    await loadedTable();

    expect(rowButton(rowOf('BTC'), 'Edit')).toHaveAccessibleName(
      exactly('Edit BTC acquired Feb 28, 2025, 11:59 PM'),
    );
    expect(rowButton(rowOf('BTC'), 'Delete')).toHaveAccessibleName(
      exactly('Delete BTC acquired Feb 28, 2025, 11:59 PM'),
    );
    expect(rowButton(rowOf('KAS'), 'Edit')).toHaveAccessibleName(
      exactly('Edit KAS acquired Jun 1, 2025, 12:00 PM'),
    );
    expect(rowButton(rowOf('ETH'), 'Delete')).toHaveAccessibleName(
      exactly('Delete ETH acquired Aug 15, 2025, 8:30 AM'),
    );
  });

  it('shows a skeleton while the list is loading, and nothing that claims an answer', async () => {
    let release: () => void = () => undefined;
    openAdjustmentsPage({
      before: ({ adjustments }) => {
        release = adjustments.hold('list');
      },
    });

    const skeleton = await screen.findByText('Loading adjustments…');

    expect(skeleton).toHaveAttribute('role', 'status');
    expect(queryTable()).toBeNull();
    expect(screen.queryByRole('heading', { name: 'No adjustments yet' })).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Could not load the adjustments' })).toBeNull();
    // The heading is there from the start: the list has a name before it has rows.
    expect(screen.getByRole('heading', { level: 3, name: 'Recorded adjustments' })).toBeVisible();

    release();
    await loadedTable();
    expect(screen.queryByText('Loading adjustments…')).toBeNull();
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
  });

  it('says "No adjustments yet" when there are none, under the list\'s own heading', async () => {
    openAdjustmentsPage({ adjustments: [] });

    const title = await screen.findByRole('heading', { level: 4, name: 'No adjustments yet' });

    expect(title.parentElement).toHaveTextContent(
      'Use the form above to record coins the imported history does not show.',
    );
    expect(queryTable()).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Loading adjustments…')).toBeNull();
  });

  it('says so, with a retry, when the first load fails: never "No adjustments yet"', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('list', () =>
          problem(500, 'Internal Server Error', 'The adjustments could not be read.'),
        );
      },
    });

    const title = await screen.findByRole('heading', {
      level: 4,
      name: 'Could not load the adjustments',
    });
    const error = title.closest('[role="alert"]');

    expect(error).toHaveTextContent('The adjustments could not be read.');
    expect(screen.queryByRole('heading', { name: 'No adjustments yet' })).toBeNull();
    expect(queryTable()).toBeNull();
    expect(screen.queryByText('Loading adjustments…')).toBeNull();

    // The retry reads again, and a list that answers replaces the error.
    adjustments.fail('list', null);
    const before = adjustments.count('list');
    await user.click(screen.getByRole('button', { name: 'Try again' }));

    await loadedTable();
    expect(adjustments.count('list')).toBeGreaterThan(before);
    expect(screen.queryByRole('heading', { name: 'Could not load the adjustments' })).toBeNull();
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
  });

  it('stays an error when the retry fails too', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await screen.findByRole('heading', { name: 'Could not load the adjustments' });
    const before = adjustments.count('list');

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => {
      expect(adjustments.count('list')).toBeGreaterThan(before);
    });
    await settle();

    expect(screen.getByRole('heading', { name: 'Could not load the adjustments' })).toBeVisible();
    expect(screen.getByText('The database is busy.')).toBeVisible();
    expect(queryTable()).toBeNull();
  });

  it('falls back to its own sentence when the backend cannot be reached at all', async () => {
    openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('list', () => HttpResponse.error());
      },
    });

    const title = await screen.findByRole('heading', { name: 'Could not load the adjustments' });

    expect(title.closest('[role="alert"]')).toHaveTextContent(
      'The backend could not be reached. Check that the API is running, then reload the page.',
    );
  });

  it('keeps the rows when a refetch fails, and says above them that they are stale', async () => {
    const { adjustments, queryClient } = openAdjustmentsPage();
    const table = await loadedTable();

    adjustments.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'adjustments'] }));

    const alert = await screen.findByText(
      'Could not refresh the adjustments: The database is busy. Showing what was last loaded.',
    );
    expect(alert).toHaveAttribute('role', 'alert');
    // The last reading is still a reading: the rows stay, under the alert.
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(precedes(alert, listRegion())).toBe(true);
    expect(listRegion()).toContainElement(table);
    expect(screen.queryByRole('heading', { name: 'Could not load the adjustments' })).toBeNull();

    // And it goes once a read succeeds again.
    adjustments.fail('list', null);
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'adjustments'] }));
    await waitFor(() => {
      expect(screen.queryByText(/^Could not refresh the adjustments/u)).toBeNull();
    });
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
  });

  it('falls back to its own words when a refetch cannot reach the server', async () => {
    const { adjustments, queryClient } = openAdjustmentsPage();
    await loadedTable();

    adjustments.fail('list', () => HttpResponse.error());
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'adjustments'] }));

    expect(
      await screen.findByText(
        'Could not refresh the adjustments: The server could not be reached. Showing what was ' +
          'last loaded.',
      ),
    ).toHaveAttribute('role', 'alert');
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
  });

  it('a save whose list refetch fails is still a save: it says so, over the stale rows', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      onChange: ({ adjustments: fake }) => {
        fake.fail('list', () => problem(503, 'Service Unavailable', 'The database is busy.'));
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.adjustments().map((entry) => entry.asset)).toContain('SOL');
    expect(
      screen.getByText(
        'Could not refresh the adjustments: The database is busy. Showing what was last loaded.',
      ),
    ).toHaveAttribute('role', 'alert');
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    // The form is reset as after any save: the owner must not be left to send it twice.
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(formErrors()).toEqual([]);
    expect(adjustments.count('create')).toBe(1);
  });
});

describe('the form is independent of the list', () => {
  it('records while the list has failed to load', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('list', () => problem(500, 'Internal Server Error', 'Could not be read.'));
      },
    });
    await screen.findByRole('heading', { name: 'Could not load the adjustments' });

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
    expect(adjustments.adjustments().map((entry) => entry.asset)).toContain('SOL');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
  });

  it('sends the adjustment while the list is still loading', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('list');
      },
    });
    await screen.findByText('Loading adjustments…');

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    // The request does not wait for the list, and what it carries is stored.
    await waitFor(() => {
      expect(adjustments.adjustments().map((entry) => entry.asset)).toContain('SOL');
    });
    expect(adjustments.count('create')).toBe(1);

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    await loadedTable();
    expect(shownAssets()).toEqual(['SOL', 'BTC', 'KAS', 'ETH']);
  });
});

describe('recording an adjustment (criterion 7)', () => {
  it('sends a POST with the five fields: amounts as strings, the date as a UTC instant', async () => {
    // Zone: pinned. 09:30 in Madrid in February is 08:30 UTC.
    inTimeZone('Europe/Madrid');
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    const request = only(adjustments.requestsTo('create'));
    expect(request.method).toBe('POST');
    expect(new URL(request.url).pathname).toBe(ADJUSTMENTS_PATH);
    expect(new URL(request.url).search).toBe('');
    expect(request.contentType).toBe('application/json');
    expect(request.body).toStrictEqual({
      asset: 'SOL',
      quantity: '2.5',
      unit_cost: '140',
      occurred_at: '2025-02-27T08:30:00.000Z',
      note: 'Bought in person.',
    });
    // On the wire, each amount is inside quotes: a JSON string, not a JSON number.
    expect(request.text).toContain('"quantity":"2.5"');
    expect(request.text).toContain('"unit_cost":"140"');
    expect(adjustments.count('replace')).toBe(0);
  });

  it.each([
    ['UTC', '2025-02-27T09:30:00.000Z'],
    ['America/New_York', '2025-02-27T14:30:00.000Z'],
    ['Asia/Kolkata', '2025-02-27T04:00:00.000Z'],
    ['Pacific/Kiritimati', '2025-02-26T19:30:00.000Z'],
  ])('in %s, 09:30 on the 27th is sent as %s', async (zone, instant) => {
    // Zone: pinned, one per case. The field is local time; what is sent is UTC.
    inTimeZone(zone);
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    expect(only(adjustments.requestsTo('create')).body).toMatchObject({ occurred_at: instant });
  });

  it('sends a summer date with the summer offset', async () => {
    // Zone: pinned. Madrid is two hours ahead of UTC in July and one in February.
    inTimeZone('Europe/Madrid');
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, occurredAt: '2025-07-15T09:30' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      occurred_at: '2025-07-15T07:30:00.000Z',
    });
  });

  it('removes the whitespace around the asset and the amounts, and sends the note as typed', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, {
      asset: '  SOL ',
      quantity: ' 2.5  ',
      unitCost: '  140 ',
      occurredAt: '2025-02-27T09:30',
    });
    // A note is the owner's words: leading spaces, a line break and a trailing space stay.
    fireEvent.change(field('Note'), { target: { value: '  Bought in person.\nPaid cash. ' } });
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      asset: 'SOL',
      quantity: '2.5',
      unit_cost: '140',
      note: '  Bought in person.\nPaid cash. ',
    });
  });

  it.each([
    ['empty', ''],
    ['only spaces', '   '],
  ])('sends a unit cost left %s as null: unknown, which is not zero', async (_label, unitCost) => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, unitCost });
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    const request = only(adjustments.requestsTo('create'));
    expect(request.body).toHaveProperty('unit_cost', null);
    expect(request.text).toContain('"unit_cost":null');
    // And the list says so in words.
    await waitFor(() => {
      expect(shownAssets()).toContain('SOL');
    });
    expect(cell(rowOf('SOL'), 'Unit cost (USD)').textContent).toBe('Unknown cost');
  });

  it('sends a cost of zero as "0": a known cost of nothing is not an unknown cost', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, unitCost: '0' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(shownAssets()).toContain('SOL');
    });

    expect(only(adjustments.requestsTo('create')).body).toHaveProperty('unit_cost', '0');
    expect(dataValues(cell(rowOf('SOL'), 'Unit cost (USD)'))).toEqual([ZERO]);
  });

  it.each([
    ['eighteen places', '3.141592653589793238', '1234.567890123456789012'],
    ['trailing zeros, which are not removed', '1.50', '20000.00'],
    ['a leading point', '.5', '.25'],
    ['more digits than a double holds', '9007199254740993', '0.1'],
  ])('sends the amounts exactly as typed: %s', async (_label, quantity, unitCost) => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, quantity, unitCost });
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    const request = only(adjustments.requestsTo('create'));
    expect(request.body).toMatchObject({ quantity, unit_cost: unitCost });
    expect(request.text).toContain(`"quantity":"${quantity}"`);
    expect(request.text).toContain(`"unit_cost":"${unitCost}"`);
  });

  it('keeps every digit of a twenty-digit quantity, from the field to the list', async () => {
    const quantity = '12345678901234567890.123456789012345678';
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, quantity, unitCost: '' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(shownAssets()).toContain('SOL');
    });

    expect(only(adjustments.requestsTo('create')).body).toHaveProperty('quantity', quantity);
    expect(dataValues(cell(rowOf('SOL'), 'Quantity'))).toEqual([quantity]);
  });

  it('says "Adjustment recorded.", empties the form and shows the new row', async () => {
    // Zone: pinned to UTC, for the new row's place in the list and its date.
    inTimeZone('UTC');
    const { user } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    // By the time the page says it is recorded, the list has read it: acquired on the 27th,
    // it replays before BTC's of the 28th.
    expect(shownAssets()).toEqual(['SOL', 'BTC', 'KAS', 'ETH']);
    const row = rowOf('SOL');
    expect(dataValues(cell(row, 'Quantity'))).toEqual(['2.500000000000000000']);
    expect(dataValues(cell(row, 'Unit cost (USD)'))).toEqual(['140.000000000000000000']);
    expect(cell(row, 'Acquired').querySelector('time')).toHaveAttribute(
      'datetime',
      '2025-02-27T09:30:00Z',
    );
    expect(cell(row, 'Note').textContent).toBe('Bought in person.');

    // An empty create form again, with focus on its heading: the button that was pressed
    // belonged to the form that was replaced.
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(submitButton()).toBeEnabled();
    expect(screen.getByRole('heading', { name: 'Record an adjustment' })).toHaveFocus();
  });

  it('disables the button while the request is pending, and sends it once', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('create');
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    expect(submitButton()).toBeEnabled();
    await user.click(submitButton());

    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    // Pressing it again, and Enter in a field, send nothing more.
    await user.click(submitButton());
    await user.type(field('Quantity'), '{Enter}');
    await settle();
    expect(adjustments.count('create')).toBe(1);
    expect(statusLine()).toBeNull();
    // What was typed stays on screen while it is being sent.
    expect(fieldValues()).toMatchObject({ Asset: 'SOL', Quantity: '2.5' });

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
    expect(submitButton()).toBeEnabled();
    expect(adjustments.adjustments().filter((entry) => entry.asset === 'SOL')).toHaveLength(1);
  });

  it('finishes a save the owner types over while it is in flight: it is said, and the form is emptied', async () => {
    // Typing clears the errors of the last attempt. It must not also let go of the request
    // that is out: the answer would land on nothing, the adjustment would be recorded, and
    // the form would neither say so nor empty itself - an invitation to record it twice.
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('create');
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    await user.type(field('Note'), ' More.');
    expect(field('Note')).toHaveValue('Bought in person. More.');
    await settle();
    expect(adjustments.count('create')).toBe(1);
    expect(submitButton()).toBeDisabled();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(submitButton()).toBeEnabled();
    expect(adjustments.count('create')).toBe(1);
    // What was recorded is what the form held when it was submitted.
    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      note: 'Bought in person.',
    });
    expect(shownAssets()).toContain('SOL');
  });

  it('submits on Enter from a field, as a form does', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.type(field('Quantity'), '{Enter}');

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
  });

  it('can record a second one straight after the first', async () => {
    const { user, adjustments } = openAdjustmentsPage({ adjustments: [] });
    await screen.findByRole('heading', { name: 'No adjustments yet' });

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(shownAssets()).toEqual(['SOL']);
    });
    await fillForm(user, { ...VALID_ENTRY, asset: 'ADA', occurredAt: '2025-03-02T10:00' });
    await user.click(submitButton());

    await waitFor(() => {
      expect(shownAssets()).toEqual(['SOL', 'ADA']);
    });
    expect(adjustments.count('create')).toBe(2);
    expect(statusLine()).toBe('Adjustment recorded.');
    expect(screen.queryByRole('heading', { name: 'No adjustments yet' })).toBeNull();
  });

  it('takes the status line down when the owner submits again', async () => {
    // The line is about the last thing that happened; an attempt is the next thing.
    const { user } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    await user.click(submitButton());

    expect(statusLine()).toBeNull();
    expect(fieldError('Asset')).toBe('An asset is required.');
  });
});

describe('editing an adjustment (criteria 7 and 8)', () => {
  it('fills the form with the adjustment and moves focus to its heading', async () => {
    // Zone: pinned to UTC, for the minute the date field shows.
    inTimeZone('UTC');
    const { user, adjustments } = openAdjustmentsPage();

    const form = await startEditing(user, 'BTC');

    expect(form).toHaveAccessibleName('Edit adjustment');
    expect(screen.getByRole('heading', { level: 3, name: 'Edit adjustment' })).toHaveFocus();
    expect(screen.queryByRole('heading', { name: 'Record an adjustment' })).toBeNull();
    expect(fieldValues()).toEqual({
      Asset: 'BTC',
      Quantity: '1.5',
      'Unit cost (USD)': '20000',
      'Acquired on': '2025-02-28T23:59',
      Note: BTC_OPENING.note,
    });
    expect(
      within(form)
        .getAllByRole('button')
        .filter((button) => !button.textContent.startsWith('Use '))
        .map((button) => button.textContent),
    ).toEqual(['Save changes', 'Cancel']);
    // Entering edit mode is not a request.
    expect(adjustments.count('replace')).toBe(0);
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
  });

  it('shows the amounts without their trailing zeros, and with every digit they have', async () => {
    const { user } = openAdjustmentsPage({
      adjustments: [
        ethPrecise(),
        adjustment({
          id: 8,
          asset: 'WBTC',
          quantity: '0.000000000000000001',
          unit_cost: '0.000000000000000001',
          occurred_at: '2025-09-01T00:00:00Z',
        }),
        adjustment({
          id: 9,
          asset: 'SHIB',
          quantity: '99999999999999999999.999999999999999999',
          unit_cost: null,
          occurred_at: '2025-10-01T00:00:00Z',
        }),
        adjustment({
          id: 10,
          asset: 'ARB',
          quantity: '1000.000000000000000000',
          unit_cost: ZERO,
          occurred_at: '2025-11-01T00:00:00Z',
        }),
      ],
    });

    await startEditing(user, 'ETH');
    expect(fieldValues()).toMatchObject({
      Quantity: '3.141592653589793238',
      'Unit cost (USD)': '1234.567890123456789012',
    });

    // The smallest amount there is: plain digits, never `1e-18`.
    await startEditing(user, 'WBTC');
    expect(fieldValues()).toMatchObject({
      Quantity: '0.000000000000000001',
      'Unit cost (USD)': '0.000000000000000001',
    });

    // The largest: every digit, never `1e+20`. And an unknown cost is an empty field.
    await startEditing(user, 'SHIB');
    expect(fieldValues()).toMatchObject({
      Quantity: '99999999999999999999.999999999999999999',
      'Unit cost (USD)': '',
    });

    // A whole number keeps its zeros before the point, and a cost of nothing reads "0":
    // an empty field would turn a known cost into an unknown one on the next save.
    await startEditing(user, 'ARB');
    expect(fieldValues()).toMatchObject({ Quantity: '1000', 'Unit cost (USD)': '0' });
  });

  it.each([
    ['UTC', '2025-02-28T23:59'],
    ['Europe/Madrid', '2025-03-01T00:59'],
    ['Asia/Kolkata', '2025-03-01T05:29'],
    ['America/New_York', '2025-02-28T18:59'],
  ])(
    'in %s, shows the stored instant to the minute and re-sends it byte for byte when untouched',
    async (zone, shown) => {
      // Zone: pinned, one per case. The stored instant has seconds and microseconds that
      // neither the field nor a `Date` can hold.
      inTimeZone(zone);
      const { user, adjustments } = openAdjustmentsPage();

      await startEditing(user, 'BTC');
      expect(field('Acquired on')).toHaveValue(shown);
      await retype(user, 'Note', 'Corrected the note only.');
      await user.click(submitButton());
      await waitFor(() => {
        expect(statusLine()).toBe('Adjustment updated.');
      });

      const request = only(adjustments.requestsTo('replace'));
      expect(request.body).toHaveProperty('occurred_at', BTC_OPENING_AT);
      expect(request.text).toContain(`"occurred_at":"${BTC_OPENING_AT}"`);
      // So the adjustment has not moved among the fills of its minute.
      expect(adjustments.adjustments()[0]).toMatchObject({ id: 1, occurred_at: BTC_OPENING_AT });
    },
  );

  it('re-sends the stored instant byte for byte when the zone changes between Edit and Save', async () => {
    // Zone: pinned, and then moved. The edit is opened in Madrid, where the field shows 00:59
    // on 1 March; by the time it is saved the browser is on New York time, where the same
    // instant is 18:59 the day before. The owner changed the note and nothing else.
    inTimeZone('Europe/Madrid');
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    expect(field('Acquired on')).toHaveValue('2025-03-01T00:59');
    await retype(user, 'Note', 'Corrected the note only.');

    inTimeZone('America/New_York');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    const request = only(adjustments.requestsTo('replace'));
    expect(request.text).toContain(`"occurred_at":"${BTC_OPENING_AT}"`);
    // Not the field's text read in the new zone: that would move it by six hours.
    expect(request.text).not.toContain('2025-03-01T05:59');
    expect(adjustments.adjustments()[0]).toMatchObject({ id: 1, occurred_at: BTC_OPENING_AT });
  });

  it('re-sends an instant on a whole minute as it is spelled, not as Date spells it', async () => {
    // `2025-08-15T08:30:00Z` round-trips through `Date` to the same instant and a different
    // string. Byte for byte means the string.
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'ETH');
    await retype(user, 'Note', 'Corrected the note only.');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(only(adjustments.requestsTo('replace')).text).toContain(
      `"occurred_at":"${ETH_ACQUIRED_AT}"`,
    );
  });

  it('sends a PUT to the adjustment with all five fields', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '1.75');
    await retype(user, 'Unit cost (USD)', '21000.5');
    await retype(user, 'Note', 'Found another receipt.');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    const request = only(adjustments.requestsTo('replace'));
    expect(request.method).toBe('PUT');
    expect(new URL(request.url).pathname).toBe(adjustmentPath(1));
    expect(request.contentType).toBe('application/json');
    expect(request.body).toStrictEqual({
      asset: 'BTC',
      quantity: '1.75',
      unit_cost: '21000.5',
      occurred_at: BTC_OPENING_AT,
      note: 'Found another receipt.',
    });
    expect(request.text).toContain('"quantity":"1.75"');
    expect(request.text).toContain('"unit_cost":"21000.5"');
    expect(adjustments.count('create')).toBe(0);
  });

  it('states an unknown cost in the PUT: unit_cost is there, and null', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'KAS');
    await retype(user, 'Note', 'Still no record of the cost.');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    const request = only(adjustments.requestsTo('replace'));
    expect(new URL(request.url).pathname).toBe(adjustmentPath(2));
    expect(request.body).toStrictEqual({
      asset: 'KAS',
      quantity: '12000',
      unit_cost: null,
      occurred_at: KAS_ACQUIRED_AT,
      note: 'Still no record of the cost.',
    });
    expect(request.text).toContain('"unit_cost":null');
  });

  it('clears a known cost to unknown, and gives an unknown one a cost', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Unit cost (USD)', '');
    await user.click(submitButton());
    await waitFor(() => {
      expect(cell(rowOf('BTC'), 'Unit cost (USD)').textContent).toBe('Unknown cost');
    });

    await startEditing(user, 'KAS');
    await retype(user, 'Unit cost (USD)', '0.05');
    await user.click(submitButton());
    await waitFor(() => {
      expect(dataValues(cell(rowOf('KAS'), 'Unit cost (USD)'))).toEqual(['0.050000000000000000']);
    });

    expect(adjustments.requestsTo('replace').map((request) => request.body)).toMatchObject([
      { asset: 'BTC', unit_cost: null },
      { asset: 'KAS', unit_cost: '0.05' },
    ]);
    // On the wire: the cleared cost is stated as null, not left out, and the quantity beside
    // it is a JSON string.
    const [cleared, priced] = adjustments.requestsTo('replace');
    expect(cleared?.text).toContain('"unit_cost":null');
    expect(cleared?.text).toContain('"quantity":"1.5"');
    expect(priced?.text).toContain('"unit_cost":"0.05"');
  });

  it('keeps a cost of zero a cost of zero through an edit that does not touch it', async () => {
    const { user, adjustments } = openAdjustmentsPage({
      adjustments: [adjustment({ asset: 'ARB', unit_cost: ZERO })],
    });

    await startEditing(user, 'ARB');
    await retype(user, 'Note', 'An airdrop.');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(only(adjustments.requestsTo('replace')).body).toHaveProperty('unit_cost', '0');
    expect(adjustments.adjustments()[0]).toMatchObject({ unit_cost: ZERO });
  });

  it('a save that changes nothing changes nothing but when it was last written', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'ETH');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(adjustments.adjustments().find((entry) => entry.id === 3)).toEqual({
      ...ethPrecise(),
      updated_at: '2026-09-24T12:00:00Z',
    });
    expect(ethPrecise().created_at).toBe(ADJUSTMENT_CREATED_AT);
  });

  it('sends a changed date as the new local time in UTC', async () => {
    // Zone: pinned. 10:15 in Madrid in February is 09:15 UTC.
    inTimeZone('Europe/Madrid');
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    setDate('2025-02-27T10:15');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(only(adjustments.requestsTo('replace')).body).toStrictEqual({
      asset: 'BTC',
      quantity: '1.5',
      unit_cost: '20000',
      occurred_at: '2025-02-27T09:15:00.000Z',
      note: BTC_OPENING.note,
    });
  });

  it('sends a date moved by one minute as that minute, with no seconds carried over', async () => {
    // Zone: pinned to UTC. The stored 23:59:37.123456 is shown as 23:59.
    inTimeZone('UTC');
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    setDate('2025-02-28T23:58');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(only(adjustments.requestsTo('replace')).body).toHaveProperty(
      'occurred_at',
      '2025-02-28T23:58:00.000Z',
    );
  });

  it('treats a date changed and changed back as untouched', async () => {
    // Zone: pinned to UTC. "Unless the owner changes the date field" is judged by what the
    // field holds when the owner saves: back on the stored minute, the stored instant goes.
    inTimeZone('UTC');
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    setDate('2025-01-01T00:00');
    setDate('2025-02-28T23:59');
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(only(adjustments.requestsTo('replace')).body).toHaveProperty(
      'occurred_at',
      BTC_OPENING_AT,
    );
  });

  it('says "Adjustment updated.", shows the change and returns to an empty create form', async () => {
    const { user } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '1.75');
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(dataValues(cell(rowOf('BTC'), 'Quantity'))).toEqual(['1.750000000000000000']);
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(within(theForm()).queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Record an adjustment' })).toHaveFocus();
  });

  it('disables "Save changes" while the request is pending, and sends it once', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('replace');
      },
    });

    await startEditing(user, 'BTC');
    await user.click(submitButton());
    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    await user.click(submitButton());
    await settle();

    expect(adjustments.count('replace')).toBe(1);
    expect(statusLine()).toBeNull();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(adjustments.count('replace')).toBe(1);
  });

  it('disables Cancel while the save is pending: a save in flight cannot be walked away from', async () => {
    // Cancel would empty the form while the request is still out; the answer would then land
    // on a form the owner believes they abandoned, and say "updated" about it.
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('replace');
      },
    });

    const form = await startEditing(user, 'BTC');
    const cancel = within(form).getByRole('button', { name: 'Cancel' });
    expect(cancel).toBeEnabled();
    await retype(user, 'Quantity', '1.75');
    await user.click(submitButton());
    await waitFor(() => {
      expect(cancel).toBeDisabled();
    });
    await user.click(cancel);
    await settle();

    // Still the edit, with what was typed: Cancel did nothing.
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(field('Quantity')).toHaveValue('1.75');

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(adjustments.adjustments()[0]).toMatchObject({ quantity: '1.750000000000000000' });
  });

  it('finishes an edit the owner types over while it is in flight', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('replace');
      },
    });

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '1.75');
    await user.click(submitButton());
    await waitFor(() => {
      expect(submitButton()).toBeDisabled();
    });
    await user.type(field('Quantity'), '9');
    expect(field('Quantity')).toHaveValue('1.759');
    await settle();

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(adjustments.count('replace')).toBe(1);
    expect(adjustments.adjustments()[0]).toMatchObject({ quantity: '1.750000000000000000' });
  });

  it('enables Cancel again after a save that failed', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('replace', () => problem(500, 'Internal Server Error', 'Not saved.'));
      },
    });

    const form = await startEditing(user, 'BTC');
    await user.click(submitButton());
    await waitFor(() => {
      expect(formErrors()).toEqual(['Not saved.']);
    });
    await user.click(within(form).getByRole('button', { name: 'Cancel' }));

    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(formErrors()).toEqual([]);
  });

  it('Cancel returns to an empty create form and sends nothing', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    const form = await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    await user.click(within(form).getByRole('button', { name: 'Cancel' }));

    expect(theForm()).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
    expect(within(theForm()).queryByRole('button', { name: 'Cancel' })).toBeNull();
    expect(screen.getByRole('heading', { name: 'Record an adjustment' })).toHaveFocus();
    await settle();
    expect(adjustments.count('replace')).toBe(0);
    expect(adjustments.count('create')).toBe(0);
    expect(statusLine()).toBeNull();
    // The row is as it was.
    expect(dataValues(cell(rowOf('BTC'), 'Quantity'))).toEqual(['1.500000000000000000']);
  });

  it('after Cancel, the next save is a create, not an edit of the row that was left', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    const form = await startEditing(user, 'BTC');
    await user.click(within(form).getByRole('button', { name: 'Cancel' }));
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
    expect(adjustments.count('replace')).toBe(0);
  });

  it('switches to another adjustment when its Edit is pressed mid-edit', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '99');
    await startEditing(user, 'ETH');

    expect(fieldValues()).toMatchObject({
      Asset: 'ETH',
      Quantity: '3.141592653589793238',
      Note: ETH_PRECISE.note,
    });
    expect(screen.getByRole('heading', { name: 'Edit adjustment' })).toHaveFocus();

    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    // The save went to ETH, with ETH's figures: nothing of the abandoned edit leaked in.
    const request = only(adjustments.requestsTo('replace'));
    expect(new URL(request.url).pathname).toBe(adjustmentPath(3));
    expect(request.body).toMatchObject({ asset: 'ETH', quantity: '3.141592653589793238' });
  });

  it('takes the status line down when an edit starts', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    await startEditing(user, 'BTC');

    expect(statusLine()).toBeNull();
  });

  it("a save of an adjustment deleted elsewhere shows the API's sentence and reads the list again", async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    adjustments.deleteElsewhere(1);
    await retype(user, 'Note', 'Too late.');
    await user.click(submitButton());

    await waitFor(() => {
      expect(formErrors()).toEqual([ADJUSTMENT_NOT_FOUND_DETAIL]);
    });
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(statusLine()).toBeNull();
    // The list is read again, so the row the owner was editing stops being there.
    await waitFor(() => {
      expect(shownAssets()).toEqual(['KAS', 'ETH']);
    });
    expect(adjustments.count('replace')).toBe(1);
    // What was typed is still in the form: it is the owner's, to copy or to cancel.
    expect(fieldValues()).toMatchObject({ Asset: 'BTC', Note: 'Too late.' });
    expect(submitButton()).toBeEnabled();
  });

  it('shows what the server refuses under the field, in edit mode as in create', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Quantity', '0');
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError('Quantity')).toBe(QUANTITY_NOT_POSITIVE_RULE);
    });
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
    expect(statusLine()).toBeNull();
    expect(adjustments.adjustments()[0]).toMatchObject({ quantity: BTC_OPENING.quantity });
  });
});

describe('what the form refuses itself (criterion 9)', () => {
  it('keeps each field described by its hint when an error joins it: the hint, then the error', async () => {
    // Zone: pinned to Europe/Madrid, which the date's hint names.
    inTimeZone('Europe/Madrid');
    const { user } = openAdjustmentsPage();
    await formReady();

    await user.click(submitButton());

    expect(fieldError('Asset')).toBe('An asset is required.');
    // What a screen reader says of a field is the texts `aria-describedby` names, in its
    // order: how to fill the field, and then what was wrong with how it was filled. The hint
    // is not what an error replaces.
    expect(field('Asset')).toHaveAccessibleDescription(
      'The symbol exactly as the exchanges spell it, in upper case, such as BTC. ' +
        'An asset is required.',
    );
    expect(field('Quantity')).toHaveAccessibleDescription(
      'Use a dot for the decimals, such as 0.5. A quantity is required.',
    );
    expect(field('Acquired on')).toHaveAccessibleDescription(
      'In your local time (Europe/Madrid). A date and time are required.',
    );
    expect(field('Note')).toHaveAccessibleDescription(
      'Why you are recording this, in your words. A note is required.',
    );
  });

  it("keeps the unit cost described by its hint beside the server's sentence about it", async () => {
    const { user } = openAdjustmentsPage();
    await formReady();

    await fillForm(user, { ...VALID_ENTRY, unitCost: '-0.01' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(fieldError('Unit cost (USD)')).toBe(UNIT_COST_NEGATIVE_RULE);
    });

    expect(field('Unit cost (USD)')).toHaveAccessibleDescription(
      'Use a dot for the decimals. Leave empty if unknown. An unknown cost is not zero: the ' +
        'units count toward the quantity held and are left out of the average cost. ' +
        UNIT_COST_NEGATIVE_RULE,
    );
  });

  it('refuses an empty form under each required field, and sends nothing', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await user.click(submitButton());

    expect(fieldErrors()).toEqual({
      Asset: 'An asset is required.',
      Quantity: 'A quantity is required.',
      // Optional: an empty cost is an unknown cost, which is something to send.
      'Unit cost (USD)': null,
      'Acquired on': 'A date and time are required.',
      Note: 'A note is required.',
    });
    expect(formErrors()).toEqual([]);
    await settle();
    expect(adjustments.count('create')).toBe(0);
    expect(statusLine()).toBeNull();
  });

  it.each<[string, Parameters<typeof fillForm>[1], FieldLabel, string]>([
    ['no asset', { asset: '' }, 'Asset', 'An asset is required.'],
    ['an asset of spaces', { asset: '   ' }, 'Asset', 'An asset is required.'],
    ['no quantity', { quantity: '' }, 'Quantity', 'A quantity is required.'],
    ['a quantity of spaces', { quantity: '  ' }, 'Quantity', 'A quantity is required.'],
    ['no date', { occurredAt: '' }, 'Acquired on', 'A date and time are required.'],
    ['no note', { note: '' }, 'Note', 'A note is required.'],
    ['a note of spaces', { note: '   ' }, 'Note', 'A note is required.'],
  ])(
    'refuses %s under that field alone, and sends nothing',
    async (_label, change, label, message) => {
      const { user, adjustments } = openAdjustmentsPage();
      await loadedTable();

      await fillForm(user, { ...VALID_ENTRY, ...change });
      await user.click(submitButton());

      expect(fieldErrors()).toEqual({ ...NO_FIELD_ERRORS, [label]: message });
      expect(formErrors()).toEqual([]);
      await settle();
      expect(adjustments.count('create')).toBe(0);
      expect(statusLine()).toBeNull();
    },
  );

  it('refuses a note that is only line breaks', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, note: '' });
    fireEvent.change(field('Note'), { target: { value: '\n\t \n' } });
    await user.click(submitButton());

    expect(fieldError('Note')).toBe('A note is required.');
    await settle();
    expect(adjustments.count('create')).toBe(0);
  });

  it('refuses a date the clock cannot represent, where toISOString would throw', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, occurredAt: '275760-09-13T00:01' });
    // The control: the input kept the value, so what follows is the form's own refusal.
    expect(field('Acquired on')).toHaveValue('275760-09-13T00:01');
    await user.click(submitButton());

    expect(fieldErrors()).toEqual({
      ...NO_FIELD_ERRORS,
      'Acquired on': 'This date is outside the range your browser can represent.',
    });
    await settle();
    expect(adjustments.count('create')).toBe(0);
    expect(statusLine()).toBeNull();
    // The page is still there: nothing threw.
    expect(theForm()).toHaveAccessibleName('Record an adjustment');
  });

  it('refuses in edit mode too: a PUT is not sent', async () => {
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    await retype(user, 'Asset', '');
    await retype(user, 'Quantity', '');
    await retype(user, 'Note', '');
    setDate('');
    await user.click(submitButton());

    expect(fieldErrors()).toEqual({
      Asset: 'An asset is required.',
      Quantity: 'A quantity is required.',
      'Unit cost (USD)': null,
      'Acquired on': 'A date and time are required.',
      Note: 'A note is required.',
    });
    await settle();
    expect(adjustments.count('replace')).toBe(0);
    expect(theForm()).toHaveAccessibleName('Edit adjustment');
  });

  it('leaves everything else to the server: a lower-case symbol and a zero are sent', async () => {
    // The form holds no copy of the server's rules. What it can send, it sends.
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, asset: 'sol', quantity: '0' });
    await user.click(submitButton());

    await waitFor(() => {
      expect(adjustments.count('create')).toBe(1);
    });
    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      asset: 'sol',
      quantity: '0',
    });
  });

  it('clears the refusals when a field changes, and sends once the form is complete', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await user.click(submitButton());
    expect(fieldError('Asset')).toBe('An asset is required.');

    // One change clears them all: they were about the last attempt, not about this one.
    await user.type(field('Asset'), 'S');
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(1);
  });

  it.each<[FieldLabel]>([['Quantity'], ['Unit cost (USD)'], ['Acquired on'], ['Note']])(
    'clears the refusals when %s changes',
    async (label) => {
      const { user } = openAdjustmentsPage();
      await loadedTable();
      await user.click(submitButton());
      expect(fieldError('Asset')).toBe('An asset is required.');

      if (label === 'Acquired on') {
        setDate('2025-02-27T09:30');
      } else {
        await user.type(field(label), '1');
      }

      expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    },
  );
});

describe('what the server refuses (criterion 9)', () => {
  it.each<[string, Parameters<typeof fillForm>[1], FieldLabel, string]>([
    ['a lower-case symbol', { asset: 'sol' }, 'Asset', ASSET_SYMBOL_RULE],
    ['a cash asset', { asset: 'USDT' }, 'Asset', CASH_ASSET_RULE],
    ['a quantity of zero', { quantity: '0' }, 'Quantity', QUANTITY_NOT_POSITIVE_RULE],
    ['a negative quantity', { quantity: '-1' }, 'Quantity', QUANTITY_NOT_POSITIVE_RULE],
    [
      'a quantity of nineteen places',
      { quantity: '0.1234567890123456789' },
      'Quantity',
      QUANTITY_TOO_PRECISE_RULE,
    ],
    [
      'a quantity of twenty-one digits',
      { quantity: '123456789012345678901', unitCost: '' },
      'Quantity',
      QUANTITY_TOO_LARGE_RULE,
    ],
    [
      'a quantity that is not a number',
      { quantity: 'abc' },
      'Quantity',
      'Input should be a valid decimal',
    ],
    ['a negative unit cost', { unitCost: '-0.01' }, 'Unit cost (USD)', UNIT_COST_NEGATIVE_RULE],
    [
      'a unit cost with a decimal comma',
      { unitCost: '12,5' },
      'Unit cost (USD)',
      'Input should be a valid decimal',
    ],
    // Later than `NOW` in every zone there is.
    [
      'a date in the future',
      { occurredAt: '2026-09-26T00:00' },
      'Acquired on',
      OCCURRED_IN_FUTURE_RULE,
    ],
  ])("shows the server's sentence for %s under that field", async (_label, change, label, rule) => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    const entry = { ...VALID_ENTRY, ...change };

    await fillForm(user, entry);
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError(label)).toBe(rule);
    });
    expect(fieldErrors()).toEqual({ ...NO_FIELD_ERRORS, [label]: rule });
    expect(formErrors()).toEqual([]);
    expect(statusLine()).toBeNull();
    // It was the server that refused: the request went, carrying what was typed.
    expect(adjustments.count('create')).toBe(1);
    // Nothing was stored, nothing was emptied, and the owner can try again.
    expect(adjustments.adjustments()).toHaveLength(3);
    expect(fieldValues()).toMatchObject({ Asset: entry.asset, Quantity: entry.quantity });
    expect(submitButton()).toBeEnabled();
    expect(shownAssets()).toEqual(['BTC', 'KAS', 'ETH']);
  });

  it('shows a note the server finds too long under the note', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, note: '' });
    fireEvent.change(field('Note'), { target: { value: 'n'.repeat(501) } });
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError('Note')).toBe(NOTE_TOO_LONG_RULE);
    });
    expect(fieldErrors()).toEqual({ ...NO_FIELD_ERRORS, Note: NOTE_TOO_LONG_RULE });
    expect(field('Note')).toHaveValue('n'.repeat(501));
  });

  it('shows two refusals at once, each under its own field', async () => {
    // The request models report every shape failure together, as Pydantic does.
    const { user } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, quantity: 'two', unitCost: 'cheap' });
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError('Quantity')).not.toBeNull();
    });
    expect(fieldErrors()).toEqual({
      ...NO_FIELD_ERRORS,
      Quantity: 'Input should be a valid decimal',
      'Unit cost (USD)': 'Input should be a valid decimal',
    });
    expect(formErrors()).toEqual([]);
  });

  it('maps each of the five fields by the last element of loc, and anything else to the bottom', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('create', () =>
          adjustmentValidationProblem([
            refusal('asset', ASSET_SYMBOL_RULE),
            refusal('quantity', QUANTITY_NOT_POSITIVE_RULE),
            refusal('unit_cost', UNIT_COST_NEGATIVE_RULE),
            refusal('occurred_at', OCCURRED_IN_FUTURE_RULE),
            refusal('note', NOTE_TOO_LONG_RULE),
            { loc: ['body'], msg: 'Input should be a valid dictionary', type: 'dict_type' },
            {
              loc: ['body', 'reason'],
              msg: 'Extra inputs are not permitted',
              type: 'extra_forbidden',
            },
          ]),
        );
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError('Asset')).toBe(ASSET_SYMBOL_RULE);
    });
    expect(fieldErrors()).toEqual({
      Asset: ASSET_SYMBOL_RULE,
      Quantity: QUANTITY_NOT_POSITIVE_RULE,
      'Unit cost (USD)': UNIT_COST_NEGATIVE_RULE,
      'Acquired on': OCCURRED_IN_FUTURE_RULE,
      Note: NOTE_TOO_LONG_RULE,
    });
    // In the order the server listed them, and not under any field.
    expect(formErrors()).toEqual([
      'Input should be a valid dictionary',
      'Extra inputs are not permitted',
    ]);
    // The general sentence is not added on top of the specific ones.
    expect(theForm()).not.toHaveTextContent(VALIDATION_DETAIL);
  });

  it('reads the field from the end of loc, however long the path to it', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('create', () =>
          adjustmentValidationProblem([
            { loc: ['body', 'adjustment', 'unit_cost'], msg: 'Nested.', type: 'value_error' },
            { loc: ['occurred_at'], msg: 'Bare.', type: 'value_error' },
            // A field's name anywhere but last is not that field.
            { loc: ['body', 'note', 'length'], msg: 'Not the note.', type: 'value_error' },
            { loc: [], msg: 'Nowhere.', type: 'value_error' },
          ]),
        );
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(fieldError('Unit cost (USD)')).toBe('Nested.');
    });
    expect(fieldErrors()).toEqual({
      ...NO_FIELD_ERRORS,
      'Unit cost (USD)': 'Nested.',
      'Acquired on': 'Bare.',
    });
    expect(formErrors()).toEqual(['Not the note.', 'Nowhere.']);
  });

  it('shows two messages with the same text at the bottom, both of them', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('create', () =>
          adjustmentValidationProblem([
            { loc: ['body', 'a'], msg: 'Extra inputs are not permitted', type: 'extra_forbidden' },
            { loc: ['body', 'b'], msg: 'Extra inputs are not permitted', type: 'extra_forbidden' },
          ]),
        );
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(formErrors()).toEqual([
        'Extra inputs are not permitted',
        'Extra inputs are not permitted',
      ]);
    });
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
  });

  it.each([
    ['with no errors member', () => problem(422, 'Unprocessable Entity', VALIDATION_DETAIL)],
    ['with an empty errors array', () => adjustmentValidationProblem([])],
    [
      // Not a shape this API writes: an index in `loc`, a `loc` that is no array, no `msg`.
      'whose every entry is malformed',
      () =>
        HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Entity',
            status: 422,
            detail: VALIDATION_DETAIL,
            errors: [{ loc: ['body', 0], msg: 'An index.' }, { loc: 'body', msg: 'A string.' }, 7],
          },
          { status: 422, headers: { 'content-type': 'application/problem+json' } },
        ),
    ],
  ])('shows the detail of a 422 %s at the bottom', async (_label, respond) => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('create', respond);
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(formErrors()).toEqual([VALIDATION_DETAIL]);
    });
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(statusLine()).toBeNull();
  });

  it.each([
    [
      "a 500, with the API's detail",
      () => problem(500, 'Internal Server Error', 'The adjustment could not be saved.'),
      'The adjustment could not be saved.',
    ],
    [
      "a 403 from the write guard, with the API's detail",
      () => problem(403, 'Forbidden', 'Writes must declare Content-Type: application/json.'),
      'Writes must declare Content-Type: application/json.',
    ],
    [
      'a network failure, with its own sentence',
      () => HttpResponse.error(),
      'Could not save the adjustment. Check your connection and try again.',
    ],
    [
      "a proxy's HTML page, with its own sentence and not the reason phrase",
      () =>
        new HttpResponse('<html><body>Bad Gateway</body></html>', {
          status: 502,
          statusText: 'Bad Gateway',
          headers: { 'content-type': 'text/html' },
        }),
      'Could not save the adjustment. Check your connection and try again.',
    ],
    [
      'truncated JSON, with its own sentence',
      () =>
        new HttpResponse('{"type":"about:blank","title":"Internal Se', {
          status: 500,
          headers: { 'content-type': 'application/problem+json' },
        }),
      'Could not save the adjustment. Check your connection and try again.',
    ],
  ])('shows %s, at the bottom of the form', async (_label, respond, message) => {
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        fake.fail('create', respond);
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(formErrors()).toEqual([message]);
    });
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(statusLine()).toBeNull();
    // Nothing is lost: the form keeps what was typed and can be sent again.
    expect(fieldValues()).toMatchObject({
      Asset: 'SOL',
      Quantity: '2.5',
      Note: 'Bought in person.',
    });
    expect(submitButton()).toBeEnabled();
    expect(adjustments.count('create')).toBe(1);
  });

  it('a 201 whose body is not JSON is a failure, said at the bottom', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail(
          'create',
          () =>
            new HttpResponse('<html></html>', {
              status: 201,
              headers: { 'content-type': 'text/html' },
            }),
        );
      },
    });
    await loadedTable();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());

    await waitFor(() => {
      expect(formErrors()).toEqual([
        'The server answered with a successful status but the body was not valid JSON.',
      ]);
    });
    expect(statusLine()).toBeNull();
  });

  it('clears every error of the last attempt when a field changes', async () => {
    const { user } = openAdjustmentsPage({
      before: ({ adjustments }) => {
        adjustments.fail('create', () =>
          adjustmentValidationProblem([
            refusal('asset', ASSET_SYMBOL_RULE),
            refusal('note', NOTE_TOO_LONG_RULE),
            { loc: ['body'], msg: 'Input should be a valid dictionary', type: 'dict_type' },
          ]),
        );
      },
    });
    await loadedTable();
    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(formErrors()).toEqual(['Input should be a valid dictionary']);
    });

    // A change to one field: the errors under the others, and at the bottom, go too.
    await user.type(field('Quantity'), '1');

    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    expect(formErrors()).toEqual([]);
  });

  it('sends again after a refusal, and records once what was refused is corrected', async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, asset: 'sol' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(fieldError('Asset')).toBe(ASSET_SYMBOL_RULE);
    });

    // Unchanged, it is sent again and refused again: the form does not remember the rule.
    await user.click(submitButton());
    await waitFor(() => {
      expect(adjustments.count('create')).toBe(2);
    });
    await waitFor(() => {
      expect(fieldError('Asset')).toBe(ASSET_SYMBOL_RULE);
    });

    await retype(user, 'Asset', 'SOL');
    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(adjustments.count('create')).toBe(3);
    expect(adjustments.adjustments().filter((entry) => entry.asset === 'SOL')).toHaveLength(1);
  });

  it("replaces the server's errors with its own when the next attempt cannot be sent", async () => {
    const { user, adjustments } = openAdjustmentsPage();
    await loadedTable();
    await fillForm(user, { ...VALID_ENTRY, asset: 'sol' });
    await user.click(submitButton());
    await waitFor(() => {
      expect(fieldError('Asset')).toBe(ASSET_SYMBOL_RULE);
    });

    await retype(user, 'Note', '');
    await user.click(submitButton());

    expect(fieldErrors()).toEqual({ ...NO_FIELD_ERRORS, Note: 'A note is required.' });
    await settle();
    expect(adjustments.count('create')).toBe(1);
  });
});

describe('the asset in the URL (criterion 11)', () => {
  it('opens the create form with the asset filled in, and nothing else', async () => {
    openAdjustmentsPage({ path: '/adjustments?asset=KAS' });

    const form = await formReady();

    expect(form).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual({ ...EMPTY_FIELDS, Asset: 'KAS' });
    expect(currentPath()).toBe('/adjustments?asset=KAS');
    expect(within(form).queryByRole('button', { name: 'Cancel' })).toBeNull();
  });

  it('reads nothing else from the URL', async () => {
    const { adjustments } = openAdjustmentsPage({
      path:
        '/adjustments?asset=BTC&quantity=0.37345678&unit_cost=20000&unitCost=20000' +
        '&occurred_at=2025-01-01T00:00&occurredAt=2025-01-01T00:00&note=From+the+URL&id=1&edit=1',
    });

    const form = await formReady();
    await loadedTable();

    // Create mode, though the URL names an id that exists; and only the asset is filled.
    expect(form).toHaveAccessibleName('Record an adjustment');
    expect(fieldValues()).toEqual({ ...EMPTY_FIELDS, Asset: 'BTC' });
    await settle();
    expect(adjustments.count('create')).toBe(0);
    expect(adjustments.count('replace')).toBe(0);
    expect(adjustments.count('delete')).toBe(0);
  });

  it.each([
    ['/adjustments?asset=A%26B', 'A&B'],
    ['/adjustments?asset=%24MYRO', '$MYRO'],
    ['/adjustments?asset=T%231', 'T#1'],
    ['/adjustments?asset=L+2', 'L 2'],
    ['/adjustments?asset=C%2B', 'C+'],
    ['/adjustments?asset=', ''],
    ['/adjustments', ''],
    ['/adjustments?other=BTC', ''],
  ])('reads %s as the asset %j', async (path, asset) => {
    openAdjustmentsPage({ path });

    await formReady();

    expect(field('Asset')).toHaveValue(asset);
  });

  it('does not prefill the date, even for an asset whose first trade is known', async () => {
    openAdjustmentsPage({ path: '/adjustments?asset=BTC' });
    await formReady();
    await screen.findByRole('button', { name: /^Use / });

    expect(field('Acquired on')).toHaveValue('');
  });

  it('is used once: after a save the form is empty, not filled from the URL again', async () => {
    const { user } = openAdjustmentsPage({ path: '/adjustments?asset=SOL' });
    await loadedTable();

    await fillForm(user, ENTRY_WITHOUT_ASSET);
    await user.click(submitButton());

    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
    expect(currentPath()).toBe('/adjustments?asset=SOL');
    expect(fieldValues()).toEqual(EMPTY_FIELDS);
  });

  it('is not put back by Cancel: Cancel returns to an empty create form', async () => {
    const { user } = openAdjustmentsPage({ path: '/adjustments?asset=SOL' });
    await formReady();
    expect(field('Asset')).toHaveValue('SOL');

    const form = await startEditing(user, 'BTC');
    expect(field('Asset')).toHaveValue('BTC');
    await user.click(within(form).getByRole('button', { name: 'Cancel' }));

    expect(fieldValues()).toEqual(EMPTY_FIELDS);
  });

  it('sends the asset of the URL as it is, with the rest typed by the owner', async () => {
    const { user, adjustments } = openAdjustmentsPage({ path: '/adjustments?asset=SOL' });
    await loadedTable();

    await fillForm(user, ENTRY_WITHOUT_ASSET);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });

    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      asset: 'SOL',
      quantity: '2.5',
    });
  });
});

describe('no amount becomes a number (criterion 16)', () => {
  it('carries eighteen places from the form, through the server, to the list and back to the form', async () => {
    // As doubles: 0.12345678901234568 and 98765.43210987655. One pass through `Number` or
    // `parseFloat` anywhere on this path shows up as a changed digit.
    const quantity = '0.123456789012345678';
    const unitCost = '98765.432109876543210987';
    const { user, adjustments } = openAdjustmentsPage({ adjustments: [] });
    await screen.findByRole('heading', { name: 'No adjustments yet' });

    await fillForm(user, { ...VALID_ENTRY, quantity, unitCost });
    await user.click(submitButton());
    await waitFor(() => {
      expect(shownAssets()).toEqual(['SOL']);
    });

    expect(only(adjustments.requestsTo('create')).body).toMatchObject({
      quantity,
      unit_cost: unitCost,
    });
    expect(dataValues(cell(rowOf('SOL'), 'Quantity'))).toEqual([quantity]);
    expect(dataValues(cell(rowOf('SOL'), 'Unit cost (USD)'))).toEqual([unitCost]);

    await startEditing(user, 'SOL');
    expect(fieldValues()).toMatchObject({ Quantity: quantity, 'Unit cost (USD)': unitCost });

    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });
    expect(only(adjustments.requestsTo('replace')).body).toMatchObject({
      quantity,
      unit_cost: unitCost,
    });
    expect(adjustments.adjustments()[0]).toMatchObject({ quantity, unit_cost: unitCost });
  });

  it('does not reorder amounts: 9 is shown before 10 when the endpoint says so', async () => {
    // A list sorted by quantity as text would put "10" before "9"; sorted as a number, 9
    // first. The endpoint's order here is neither, and it is the one shown.
    openAdjustmentsPage({
      adjustments: [
        adjustment({ id: 1, asset: 'TEN', quantity: '10.000000000000000000' }),
        adjustment({ id: 2, asset: 'NINE', quantity: '9.000000000000000000' }),
        adjustment({ id: 3, asset: 'HUNDRED', quantity: '100.000000000000000000' }),
      ],
    });
    await loadedTable();

    expect(shownAssets()).toEqual(['TEN', 'NINE', 'HUNDRED']);
  });
});

describe('the status line', () => {
  it('is one role="status" paragraph between the form and the list, there from the start and empty', async () => {
    // A live region is announced when its text changes, and reliably only if the region was
    // already in the document: so the paragraph is always there, and its text is swapped.
    const { user } = openAdjustmentsPage();
    await loadedTable();

    const [line, ...others] = statusElements();
    expect(others).toEqual([]);
    expect(line?.tagName).toBe('P');
    expect(line?.textContent).toBe('');
    expect(line !== undefined && precedes(theForm(), line)).toBe(true);
    expect(
      line !== undefined &&
        precedes(line, screen.getByRole('heading', { name: 'Recorded adjustments' })),
    ).toBe(true);

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await screen.findByText('Adjustment recorded.');

    // The same element says it: nothing was mounted to say it.
    expect(statusElements()).toEqual([line]);
    expect(line?.textContent).toBe('Adjustment recorded.');
  });

  it('is the same element through a save, an edit and a refusal, emptied in between', async () => {
    const { user } = openAdjustmentsPage();
    await loadedTable();
    const [line] = statusElements();

    await fillForm(user, VALID_ENTRY);
    await user.click(submitButton());
    await waitFor(() => {
      expect(line?.textContent).toBe('Adjustment recorded.');
    });

    await startEditing(user, 'BTC');
    expect(line?.textContent).toBe('');
    await user.click(submitButton());
    await waitFor(() => {
      expect(line?.textContent).toBe('Adjustment updated.');
    });

    await user.click(submitButton());
    expect(line?.textContent).toBe('');
    expect(statusElements()).toEqual([line]);
  });
});
