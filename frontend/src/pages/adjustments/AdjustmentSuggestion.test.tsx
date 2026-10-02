import { act, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NOW } from '@/test/accountingFixtures';
import {
  BTC_FIRST_TRADE_AT,
  ETH_FIRST_TRADE_AT,
  firstTrade,
  firstTrades,
} from '@/test/adjustmentFixtures';
import {
  exactly,
  field,
  fieldError,
  fieldErrors,
  fillForm,
  formErrors,
  formReady,
  loadedTable,
  NO_FIELD_ERRORS,
  openAdjustmentsPage,
  precedes,
  retype,
  setDate,
  spaced,
  startEditing,
  statusLine,
  submitButton,
  theForm,
  VALID_ENTRY,
} from '@/test/adjustmentsPage';
import { settle } from '@/test/render';
import { problem } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The date the form suggests, and the assets it offers (spec 027, "The suggested date";
 * acceptance criterion 10), rendered inside the whole app at `/adjustments`.
 *
 * **Time zones.** The suggestion is local midnight of a local day, so every test that asserts
 * on a time, a label or a field value names its zone with `inTimeZone` before the page is
 * opened. The tests that only ask whether the hint is there hold in any zone.
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

/** The hint under the date field, or `null` when there is none. */
function queryHint(): HTMLElement | null {
  return within(theForm()).queryByText(
    (_text, element) =>
      element?.tagName === 'P' && element.textContent.startsWith('The earliest imported trade of'),
  );
}

interface Hint {
  /** The sentence the hint says. */
  readonly sentence: string;
  /** The `dateTime` of the `<time>` the earliest trade is shown in. */
  readonly instant: string | null;
  /** The "Use <date>" button beside it. */
  readonly button: HTMLElement;
}

/** The button that sets the suggested date, or `null` when there is none. */
function queryUseButton(): HTMLElement | null {
  return within(theForm()).queryByRole('button', { name: /^Use / });
}

/** The hint, taken apart. Fails when there is none, or when it has no button beside it. */
function hint(): Hint {
  const paragraph = queryHint();
  if (paragraph === null) {
    throw new Error('The form shows no hint about the earliest trade.');
  }
  const button = queryUseButton();
  if (button === null) {
    throw new Error('The hint about the earliest trade has no "Use <date>" button.');
  }

  return {
    sentence: spaced(paragraph.textContent),
    instant: paragraph.querySelector('time')?.getAttribute('datetime') ?? null,
    button,
  };
}

/** The sentence, for an asset and the instant of its earliest trade as it is read. */
function sentenceFor(asset: string, tradeRead: string): string {
  return (
    `The earliest imported trade of ${asset} is ${tradeRead}. Coins you already held by then ` +
    'should be dated before it; coins acquired later should carry the date you acquired them.'
  );
}

/** The hint, once `first-trades` has answered and the asset typed is one of its assets. */
async function hintShown(): Promise<Hint> {
  await waitFor(() => {
    expect(queryHint()).not.toBeNull();
  });
  return hint();
}

/** The values the asset field's datalist offers, or `null` when it has no datalist. */
function offeredAssets(): string[] | null {
  const id = field('Asset').getAttribute('list');
  const list = id === null ? null : document.getElementById(id);
  if (list === null) {
    return null;
  }
  expect(list.tagName).toBe('DATALIST');
  return Array.from(list.querySelectorAll('option')).map((option) => option.value);
}

describe('the suggested date (criterion 10)', () => {
  it('says when the earliest imported trade of the asset is, and which coins to date before it', async () => {
    // Zone: pinned. 10:00:37 UTC is 11:00 in Madrid in March.
    inTimeZone('Europe/Madrid');
    const { user } = openAdjustmentsPage();
    await formReady();

    await user.type(field('Asset'), 'BTC');
    const shown = await hintShown();

    expect(shown.sentence).toBe(
      'The earliest imported trade of BTC is Mar 1, 2025, 11:00 AM. Coins you already held by ' +
        'then should be dated before it; coins acquired later should carry the date you ' +
        'acquired them.',
    );
    expect(shown.sentence).toBe(sentenceFor('BTC', 'Mar 1, 2025, 11:00 AM'));
    // An instant, as the server sent it: an absolute time, not a phrase that ticks.
    expect(shown.instant).toBe(BTC_FIRST_TRADE_AT);
    expect(shown.sentence).not.toMatch(/ago|just now/u);
    expect(spaced(shown.button.textContent)).toBe('Use Feb 28, 2025, 12:00 AM');
  });

  it('puts the hint under the date field with its button beside it, and ties the field to it', async () => {
    const { user } = openAdjustmentsPage();
    await formReady();

    await user.type(field('Asset'), 'BTC');
    const shown = await hintShown();
    const paragraph = queryHint();
    const date = field('Acquired on');

    expect(paragraph?.parentElement).toBe(date.parentElement);
    expect(paragraph !== null && precedes(date, paragraph)).toBe(true);
    // The button comes straight after the sentence that explains it, and is not part of it:
    // a button the size of a line of hint text is a small target on a phone.
    expect(paragraph?.nextElementSibling).toBe(shown.button);
    expect(paragraph).not.toContainElement(shown.button);
    expect(shown.button).toHaveAttribute('type', 'button');
    expect(date).toHaveAccessibleDescription(
      expect.stringContaining('The earliest imported trade of BTC is'),
    );
    // Advice is not an error.
    expect(paragraph).not.toHaveAttribute('role', 'alert');
    expect(fieldError('Acquired on')).toBeNull();
  });

  it('describes the date by its hint, then the suggestion, then an error: each kept, in that order', async () => {
    // Zone: pinned to Europe/Madrid, which the hint names and the earliest trade is read in.
    inTimeZone('Europe/Madrid');
    const { user } = openAdjustmentsPage();
    await formReady();
    const zoneHint = 'In your local time (Europe/Madrid).';
    expect(field('Acquired on')).toHaveAccessibleDescription(zoneHint);

    await user.type(field('Asset'), 'BTC');
    await hintShown();
    const advised = `${zoneHint} ${sentenceFor('BTC', 'Mar 1, 2025, 11:00 AM')}`;
    expect(field('Acquired on')).toHaveAccessibleDescription(exactly(advised));

    // The date is left empty, which the form refuses: the error joins the two, last.
    await user.click(submitButton());
    expect(fieldError('Acquired on')).toBe('A date and time are required.');
    expect(field('Acquired on')).toHaveAccessibleDescription(
      exactly(`${advised} A date and time are required.`),
    );

    // The asset is changed to one with no history: the suggestion leaves the description.
    await user.type(field('Asset'), 'X');
    await user.click(submitButton());
    expect(field('Acquired on')).toHaveAccessibleDescription(
      `${zoneHint} A date and time are required.`,
    );
  });

  it.each([
    // [what it shows, zone, earliest trade, the trade as read, the button, the field, sent]
    [
      'across a month boundary',
      'Europe/Madrid',
      BTC_FIRST_TRADE_AT,
      'Mar 1, 2025, 11:00 AM',
      'Feb 28, 2025, 12:00 AM',
      '2025-02-28T00:00',
      '2025-02-27T23:00:00.000Z',
    ],
    [
      'onto a leap day',
      'UTC',
      '2024-03-01T10:00:37Z',
      'Mar 1, 2024, 10:00 AM',
      'Feb 29, 2024, 12:00 AM',
      '2024-02-29T00:00',
      '2024-02-29T00:00:00.000Z',
    ],
    [
      'across a year boundary, from a trade at midnight exactly',
      'UTC',
      '2025-01-01T00:00:00Z',
      'Jan 1, 2025, 12:00 AM',
      'Dec 31, 2024, 12:00 AM',
      '2024-12-31T00:00',
      '2024-12-31T00:00:00.000Z',
    ],
    [
      // The day before is the 23-hour day. Midnight minus 24 hours is 23:00 on the 29th.
      'onto the day the clocks go forward',
      'Europe/Madrid',
      '2025-03-31T08:00:00Z',
      'Mar 31, 2025, 10:00 AM',
      'Mar 30, 2025, 12:00 AM',
      '2025-03-30T00:00',
      '2025-03-29T23:00:00.000Z',
    ],
    [
      // The day before is the 25-hour day. Midnight minus 24 hours is 01:00 on the 26th.
      'onto the day the clocks go back',
      'Europe/Madrid',
      '2025-10-27T09:00:00Z',
      'Oct 27, 2025, 10:00 AM',
      'Oct 26, 2025, 12:00 AM',
      '2025-10-26T00:00',
      '2025-10-25T22:00:00.000Z',
    ],
    [
      'across a month boundary and a clock change at once',
      'Europe/Madrid',
      '2024-04-01T08:00:00Z',
      'Apr 1, 2024, 10:00 AM',
      'Mar 31, 2024, 12:00 AM',
      '2024-03-31T00:00',
      '2024-03-30T23:00:00.000Z',
    ],
    [
      'onto the day the clocks go back, west of UTC',
      'America/New_York',
      '2025-11-03T15:00:00Z',
      'Nov 3, 2025, 10:00 AM',
      'Nov 2, 2025, 12:00 AM',
      '2025-11-02T00:00',
      '2025-11-02T04:00:00.000Z',
    ],
    [
      // In Santiago the clocks jump at midnight: that day begins at 01:00, and the form says so.
      'onto a day that has no midnight',
      'America/Santiago',
      '2026-09-07T15:00:00Z',
      'Sep 7, 2026, 12:00 PM',
      'Sep 6, 2026, 1:00 AM',
      '2026-09-06T01:00',
      '2026-09-06T04:00:00.000Z',
    ],
    [
      // The trade's local day is the 2nd, though its UTC day is the 1st.
      'by the local day, far east of UTC',
      'Pacific/Kiritimati',
      BTC_FIRST_TRADE_AT,
      'Mar 2, 2025, 12:00 AM',
      'Mar 1, 2025, 12:00 AM',
      '2025-03-01T00:00',
      '2025-02-28T10:00:00.000Z',
    ],
    [
      // The trade's local day is February 28, though its UTC day is March 1.
      'by the local day, west of UTC',
      'America/Los_Angeles',
      '2025-03-01T05:00:00Z',
      'Feb 28, 2025, 9:00 PM',
      'Feb 27, 2025, 12:00 AM',
      '2025-02-27T00:00',
      '2025-02-27T08:00:00.000Z',
    ],
    [
      // Ruling R3: `first_trade_at` is the stored instant, and can carry a fraction.
      'from a trade stamped with a fraction of a second',
      'UTC',
      ETH_FIRST_TRADE_AT,
      'Jul 4, 2025, 4:45 PM',
      'Jul 3, 2025, 12:00 AM',
      '2025-07-03T00:00',
      '2025-07-03T00:00:00.000Z',
    ],
  ])(
    'the button sets local midnight of the day before, %s',
    async (_label, zone, firstTradeAt, tradeRead, label, value, sent) => {
      // Zone: pinned, one per case.
      inTimeZone(zone);
      const { user, adjustments } = openAdjustmentsPage({
        firstTrades: firstTrades([firstTrade('BTC', firstTradeAt)]),
      });
      await loadedTable();

      await fillForm(user, { asset: 'BTC', quantity: '0.5', unitCost: '', note: 'Opening.' });
      const shown = await hintShown();
      expect(shown.sentence).toBe(sentenceFor('BTC', tradeRead));
      expect(shown.instant).toBe(firstTradeAt);
      expect(spaced(shown.button.textContent)).toBe(`Use ${label}`);
      // A button, never a prefill: the field is empty until it is pressed.
      expect(field('Acquired on')).toHaveValue('');

      await user.click(shown.button);

      expect(field('Acquired on')).toHaveValue(value);
      // The owner is dating the adjustment: focus goes to the field they can now adjust.
      expect(field('Acquired on')).toHaveFocus();
      // Pressing it sets a field. It is not a submit.
      await settle();
      expect(adjustments.count('create')).toBe(0);

      // And what the field now holds is sent as that local midnight, in UTC.
      await user.click(submitButton());
      await waitFor(() => {
        expect(statusLine()).toBe('Adjustment recorded.');
      });
      expect(adjustments.requestsTo('create').map((request) => request.body)).toMatchObject([
        { asset: 'BTC', occurred_at: sent },
      ]);
    },
  );

  it.each([
    ['BTC', true],
    ['  BTC ', true],
    ['1INCH', true],
    ['btc', false],
    ['Btc', false],
    ['BT', false],
    ['BTCX', false],
    ['B TC', false],
    ['DOGE', false],
    ['   ', false],
  ])('for the asset %j, the hint is shown: %s', async (typed, expected) => {
    const { user, adjustments } = openAdjustmentsPage({
      firstTrades: firstTrades([firstTrade('1INCH'), firstTrade('BTC')]),
    });
    await formReady();
    await waitFor(() => {
      expect(offeredAssets()).toEqual(['1INCH', 'BTC']);
    });

    await user.type(field('Asset'), typed);

    expect(queryHint() !== null).toBe(expected);
    if (expected) {
      // It names the asset as the exchanges spell it, without the spaces around what was typed.
      expect(hint().sentence).toContain(`The earliest imported trade of ${typed.trim()} is`);
    } else {
      expect(queryUseButton()).toBeNull();
    }
    expect(adjustments.count('create')).toBe(0);
  });

  it('matches the asset exactly as first-trades spells it: lower case is another asset', async () => {
    // `first-trades` spells an asset as the exchange does. One listed in lower case is matched
    // by lower case and not by upper.
    const { user } = openAdjustmentsPage({
      firstTrades: firstTrades([firstTrade('BTC'), firstTrade('wbtc', ETH_FIRST_TRADE_AT)]),
    });
    await formReady();
    await waitFor(() => {
      expect(offeredAssets()).toEqual(['BTC', 'wbtc']);
    });

    await user.type(field('Asset'), 'WBTC');
    expect(queryHint()).toBeNull();

    await retype(user, 'Asset', 'wbtc');
    expect(hint().instant).toBe(ETH_FIRST_TRADE_AT);
  });

  it('shows nothing with no asset typed, and no date is ever filled in for the owner', async () => {
    const { user } = openAdjustmentsPage();
    await formReady();
    await waitFor(() => {
      expect(offeredAssets()).not.toBeNull();
    });

    expect(queryHint()).toBeNull();
    expect(field('Acquired on')).toHaveValue('');

    await user.type(field('Asset'), 'BTC');
    await hintShown();
    // The form cannot know whether this is an opening balance or a later acquisition.
    expect(field('Acquired on')).toHaveValue('');
  });

  it('follows the asset as it is typed, and leaves a date already set alone', async () => {
    // Zone: pinned to UTC.
    inTimeZone('UTC');
    const { user } = openAdjustmentsPage();
    await formReady();

    await user.type(field('Asset'), 'ETH');
    const eth = await hintShown();
    expect(eth.instant).toBe(ETH_FIRST_TRADE_AT);
    await user.click(eth.button);
    expect(field('Acquired on')).toHaveValue('2025-07-03T00:00');

    // Another asset: another trade, another button. The field is not touched by the change.
    await retype(user, 'Asset', 'KAS');
    expect(hint().sentence).toContain('The earliest imported trade of KAS is Nov 2, 2025, 5:30 AM');
    expect(spaced(hint().button.textContent)).toBe('Use Nov 1, 2025, 12:00 AM');
    expect(field('Acquired on')).toHaveValue('2025-07-03T00:00');

    // An asset with no imported trade: no hint, and still the same date.
    await user.type(field('Asset'), 'X');
    expect(queryHint()).toBeNull();
    expect(queryUseButton()).toBeNull();
    expect(field('Acquired on')).toHaveValue('2025-07-03T00:00');
    expect(field('Acquired on')).not.toHaveAccessibleDescription(
      expect.stringContaining('The earliest imported trade'),
    );
  });

  it('replaces a date the owner had typed when the button is pressed', async () => {
    // Zone: pinned to UTC.
    inTimeZone('UTC');
    const { user } = openAdjustmentsPage();
    await formReady();

    await user.type(field('Asset'), 'BTC');
    setDate('2025-06-15T12:34');
    await user.click((await hintShown()).button);

    expect(field('Acquired on')).toHaveValue('2025-02-28T00:00');
  });

  it('clears the errors of the last attempt, as any change to a field does', async () => {
    const { user } = openAdjustmentsPage();
    await formReady();
    await user.type(field('Asset'), 'BTC');
    await hintShown();
    await user.click(submitButton());
    expect(fieldError('Acquired on')).toBe('A date and time are required.');
    expect(fieldError('Quantity')).toBe('A quantity is required.');

    await user.click(hint().button);

    expect(fieldErrors()).toEqual(NO_FIELD_ERRORS);
  });

  it('is offered in edit mode too, where pressing it is a change of date', async () => {
    // Zone: pinned to UTC. BTC's adjustment is stored at 23:59:37.123456 on February 28.
    inTimeZone('UTC');
    const { user, adjustments } = openAdjustmentsPage();

    await startEditing(user, 'BTC');
    const shown = await hintShown();
    expect(spaced(shown.button.textContent)).toBe('Use Feb 28, 2025, 12:00 AM');
    await user.click(shown.button);
    await user.click(submitButton());
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment updated.');
    });

    expect(adjustments.requestsTo('replace').map((request) => request.body)).toMatchObject([
      { asset: 'BTC', occurred_at: '2025-02-28T00:00:00.000Z' },
    ]);
  });
});

describe('the assets the form offers', () => {
  it('offers the assets of first-trades, in the order served, through a datalist', async () => {
    openAdjustmentsPage();
    await formReady();

    await waitFor(() => {
      expect(offeredAssets()).toEqual(['BTC', 'ETH', 'KAS']);
    });
    // A suggestion, not a restriction: the field is still free text.
    expect(field('Asset')).toHaveAttribute('type', 'text');
  });

  it('offers nothing to an owner with no imported trades, and suggests no date', async () => {
    const { user, adjustments } = openAdjustmentsPage({ firstTrades: firstTrades() });
    await formReady();
    await waitFor(() => {
      expect(adjustments.count('first-trades')).toBeGreaterThan(0);
    });
    await settle();

    await user.type(field('Asset'), 'BTC');

    expect(offeredAssets()).toEqual([]);
    expect(queryHint()).toBeNull();
    expect(queryUseButton()).toBeNull();
  });
});

describe('without first-trades, the form is the same form', () => {
  it('shows no hint and no datalist while first-trades is pending, and sends what is typed', async () => {
    let release: () => void = () => undefined;
    const { user, adjustments } = openAdjustmentsPage({
      before: ({ adjustments: fake }) => {
        release = fake.hold('first-trades');
      },
    });
    await loadedTable();

    await fillForm(user, { ...VALID_ENTRY, asset: 'BTC' });

    expect(queryHint()).toBeNull();
    expect(offeredAssets()).toBeNull();
    expect(queryUseButton()).toBeNull();
    expect(formErrors()).toEqual([]);

    await user.click(submitButton());
    // The save does not wait for the read it does not need.
    await waitFor(() => {
      expect(adjustments.adjustments().filter((entry) => entry.asset === 'BTC')).toHaveLength(2);
    });
    expect(adjustments.count('create')).toBe(1);

    release();
    await waitFor(() => {
      expect(statusLine()).toBe('Adjustment recorded.');
    });
  });

  it.each([
    ['answers 500', () => problem(500, 'Internal Server Error', 'The fills could not be read.')],
    ['cannot be reached', () => HttpResponse.error()],
  ])(
    'shows no hint, no datalist and no error when first-trades %s, and records',
    async (_label, respond) => {
      const { user, adjustments } = openAdjustmentsPage({
        before: ({ adjustments: fake }) => {
          fake.fail('first-trades', respond);
        },
      });
      await loadedTable();
      await waitFor(() => {
        expect(adjustments.count('first-trades')).toBeGreaterThan(0);
      });
      await settle();

      await fillForm(user, { ...VALID_ENTRY, asset: 'BTC' });

      expect(queryHint()).toBeNull();
      expect(offeredAssets()).toBeNull();
      // A hint that could not be read is not the owner's problem: nothing is said about it.
      expect(screen.queryByRole('alert')).toBeNull();

      await user.click(submitButton());
      await waitFor(() => {
        expect(statusLine()).toBe('Adjustment recorded.');
      });
      expect(adjustments.count('create')).toBe(1);
    },
  );

  it('keeps the hint and the datalist when a later read of first-trades fails', async () => {
    // What the last good read said is still true: the earliest trade of an asset only moves
    // when a sync imports older fills, and a sync reads this again.
    const { user, adjustments, queryClient } = openAdjustmentsPage();
    await formReady();
    await user.type(field('Asset'), 'BTC');
    await hintShown();
    const before = adjustments.count('first-trades');

    adjustments.fail('first-trades', () => problem(503, 'Service Unavailable', 'Busy.'));
    await act(() => queryClient.invalidateQueries({ queryKey: ['accounting', 'first-trades'] }));
    await waitFor(() => {
      expect(adjustments.count('first-trades')).toBeGreaterThan(before);
    });
    await settle();

    expect(hint().instant).toBe(BTC_FIRST_TRADE_AT);
    expect(offeredAssets()).toEqual(['BTC', 'ETH', 'KAS']);
    expect(within(theForm()).queryByRole('alert')).toBeNull();
  });
});
