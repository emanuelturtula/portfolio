import { describe, expect, it } from 'vitest';

import type { Adjustment } from '@/api/adjustments';
import { ApiError, type FieldError, type ProblemDetails } from '@/api/client';
import {
  adjustmentName,
  ADJUSTMENTS_ROUTE,
  adjustmentsRouteFor,
  ASSET_REQUIRED,
  DATE_REQUIRED,
  DATE_UNREPRESENTABLE,
  emptyFormValues,
  firstTradeOf,
  formValuesOf,
  mapAdjustmentError,
  NOTE_REQUIRED,
  prepareSubmission,
  QUANTITY_REQUIRED,
  SAVE_FAILURE,
  suggestedDate,
  toLocalInputValue,
  type AdjustmentFormValues,
  type Submission,
} from '@/lib/adjustments';
import {
  adjustment,
  ADJUSTMENT_NOT_FOUND_DETAIL,
  ASSET_SYMBOL_RULE,
  BTC_FIRST_TRADE_AT,
  BTC_OPENING_AT,
  CASH_ASSET_RULE,
  ethPrecise,
  firstTrade,
  firstTrades,
  kasUnknownCost,
  NOTE_BLANK_RULE,
  NOTE_TOO_LONG_RULE,
  OCCURRED_IN_FUTURE_RULE,
  QUANTITY_NOT_POSITIVE_RULE,
  threeFirstTrades,
  TOTAL_COST_TOO_LARGE_RULE,
  UNIT_COST_NEGATIVE_RULE,
  VALIDATION_DETAIL,
} from '@/test/adjustmentFixtures';
import { inTimeZone } from '@/test/timeZone';

/**
 * Spec 027, "What is sent", "Validation" and "The suggested date": everything about the
 * adjustments page that is not React.
 *
 * **Time zones.** A `datetime-local` value is the browser's local time, so every test that
 * turns one into an instant, or an instant into one, pins the zone with `inTimeZone` and says
 * which. The tests that pin none do not depend on it. Every expected instant is written out
 * by hand from the zone's rules, never computed with `Date`: the code under test is `Date`
 * arithmetic, and an expectation built the same way would agree with any bug in it.
 *
 * **Money.** Every amount is a string from the first byte to the last. None is parsed here.
 */

/**
 * Newer ICU puts a narrow no-break space (U+202F) before AM/PM. The DOM matchers collapse it
 * with every other space, so the pure tests do too.
 */
function plain(text: string): string {
  return text.replace(/\s+/gu, ' ');
}

/** A filled form, as an owner would have typed it. */
function typed(overrides: Partial<AdjustmentFormValues> = {}): AdjustmentFormValues {
  return {
    asset: 'BTC',
    quantity: '1.5',
    unitCost: '20000',
    occurredAt: '2025-02-28T09:30',
    note: 'Opening balance.',
    ...overrides,
  };
}

/**
 * `prepareSubmission` as the form calls it. Its third argument is what the date field held
 * when the form was mounted: empty for a create form, and the stored instant's local minute
 * for an edit, worked out in the zone the test is in when this is called. The tests of a zone
 * that changes between mount and submit call `prepareSubmission` themselves.
 */
function submitted(values: AdjustmentFormValues, editing: Adjustment | null): Submission {
  return prepareSubmission(
    values,
    editing,
    editing === null ? '' : toLocalInputValue(editing.occurred_at),
  );
}

/** A 422 as `api/client.ts` reads the backend's: the problem document and its `errors`. */
function validationError(errors: readonly FieldError[]): ApiError {
  return new ApiError(
    {
      type: 'about:blank',
      title: 'Unprocessable Entity',
      status: 422,
      detail: VALIDATION_DETAIL,
      errors,
    },
    true,
  );
}

function body(field: string, msg: string): FieldError {
  return { loc: ['body', field], msg };
}

function problemError(problem: Partial<ProblemDetails> & { status: number }): ApiError {
  return new ApiError({ type: 'about:blank', title: 'Error', ...problem }, true);
}

describe('the route', () => {
  it('is /adjustments', () => {
    expect(ADJUSTMENTS_ROUTE).toBe('/adjustments');
  });

  it('carries the asset in the query string', () => {
    expect(adjustmentsRouteFor('BTC')).toBe('/adjustments?asset=BTC');
    expect(adjustmentsRouteFor('1000SATS')).toBe('/adjustments?asset=1000SATS');
  });

  it.each([
    ['A&B', '/adjustments?asset=A%26B'],
    ['BTC#1', '/adjustments?asset=BTC%231'],
    ['A=B', '/adjustments?asset=A%3DB'],
    ['A B', '/adjustments?asset=A+B'],
    ['A+B', '/adjustments?asset=A%2BB'],
    ['A?B', '/adjustments?asset=A%3FB'],
    ['Ã', '/adjustments?asset=%C3%83'],
    ['50%', '/adjustments?asset=50%25'],
  ])('encodes the asset %j, so that it cannot change what the link means', (asset, href) => {
    // A symbol is data. Concatenated, `A&B` would be an asset `A` and a parameter `B`, and
    // `BTC#1` would be an asset `BTC` and a fragment.
    expect(adjustmentsRouteFor(asset)).toBe(href);
    // And the page reads back exactly what was put in.
    const query = adjustmentsRouteFor(asset).slice(ADJUSTMENTS_ROUTE.length + 1);
    expect(new URLSearchParams(query).get('asset')).toBe(asset);
    expect([...new URLSearchParams(query).keys()]).toEqual(['asset']);
  });
});

describe('emptyFormValues', () => {
  it('is an empty create form', () => {
    expect(emptyFormValues('')).toEqual({
      asset: '',
      quantity: '',
      unitCost: '',
      occurredAt: '',
      note: '',
    });
  });

  it('fills the asset the URL carried, and nothing else', () => {
    expect(emptyFormValues('BTC')).toEqual({
      asset: 'BTC',
      quantity: '',
      unitCost: '',
      occurredAt: '',
      note: '',
    });
  });
});

describe('toLocalInputValue', () => {
  it('is the instant to the minute, in UTC when the browser is in UTC', () => {
    inTimeZone('UTC');
    expect(toLocalInputValue('2025-06-01T12:00:05Z')).toBe('2025-06-01T12:00');
    expect(toLocalInputValue('2025-08-15T08:30:00Z')).toBe('2025-08-15T08:30');
  });

  it('is local time, not UTC: east of Greenwich it can be the next day and month', () => {
    inTimeZone('Europe/Madrid');
    // Winter: CET, UTC+1. 23:59 UTC on 28 February is 00:59 on 1 March.
    expect(toLocalInputValue(BTC_OPENING_AT)).toBe('2025-03-01T00:59');
    // Summer: CEST, UTC+2.
    expect(toLocalInputValue('2025-08-15T08:30:00Z')).toBe('2025-08-15T10:30');
  });

  it('is local time west of Greenwich, where it can be the day before', () => {
    inTimeZone('America/New_York');
    // EST, UTC-5.
    expect(toLocalInputValue('2025-03-01T03:15:00Z')).toBe('2025-02-28T22:15');
    // EDT, UTC-4.
    expect(toLocalInputValue('2025-08-15T08:30:00Z')).toBe('2025-08-15T04:30');
  });

  it('follows an offset that is not a whole hour', () => {
    inTimeZone('Asia/Kolkata');
    // UTC+5:30.
    expect(toLocalInputValue('2025-06-01T12:00:05Z')).toBe('2025-06-01T17:30');
  });

  it('drops the seconds and the fraction, and never rounds them up', () => {
    inTimeZone('UTC');
    // 23:59:59.999999 is still in minute 59: rounding would show the next day.
    expect(toLocalInputValue('2025-12-31T23:59:59.999999Z')).toBe('2025-12-31T23:59');
    expect(toLocalInputValue('2025-02-28T23:59:37.123456Z')).toBe('2025-02-28T23:59');
    expect(toLocalInputValue('2025-02-28T23:59:30Z')).toBe('2025-02-28T23:59');
  });

  it('pads every part to the width a datetime-local value has', () => {
    inTimeZone('UTC');
    expect(toLocalInputValue('2025-01-02T03:04:05Z')).toBe('2025-01-02T03:04');
    expect(toLocalInputValue('0999-01-02T03:04:05Z')).toBe('0999-01-02T03:04');
  });
});

describe('formValuesOf', () => {
  it('fills the five fields from a stored adjustment', () => {
    inTimeZone('UTC');
    expect(formValuesOf(adjustment())).toEqual({
      asset: 'BTC',
      quantity: '1.5',
      unitCost: '20000',
      occurredAt: '2025-02-28T23:59',
      note: 'Opening balance: bought before the exchange history begins.',
    });
  });

  it('shows an unknown cost as an empty field, never as a zero', () => {
    inTimeZone('UTC');
    const values = formValuesOf(kasUnknownCost());

    expect(values.unitCost).toBe('');
    expect(values.quantity).toBe('12000');
  });

  it('shows a cost of zero as "0": a known cost of nothing is not an unknown one', () => {
    inTimeZone('UTC');
    expect(formValuesOf(adjustment({ unit_cost: '0.000000000000000000' })).unitCost).toBe('0');
  });

  it('keeps every one of eighteen places a double cannot hold', () => {
    inTimeZone('UTC');
    // As doubles these are 3.141592653589793 and 1234.5678901234568.
    const values = formValuesOf(ethPrecise());

    expect(values.quantity).toBe('3.141592653589793238');
    expect(values.unitCost).toBe('1234.567890123456789012');
  });

  it('never writes an amount with an exponent', () => {
    inTimeZone('UTC');
    const values = formValuesOf(
      adjustment({
        quantity: '0.000000000000000001',
        unit_cost: '12345678901234567890.000000000000000000',
      }),
    );

    expect(values.quantity).toBe('0.000000000000000001');
    expect(values.unitCost).toBe('12345678901234567890');
  });

  it('keeps the note exactly as stored, surrounding whitespace and line breaks included', () => {
    inTimeZone('UTC');
    const note = '  Bought in two lots.\n\nSecond lot in cash.  ';

    expect(formValuesOf(adjustment({ note })).note).toBe(note);
  });

  it('shows the date in local time', () => {
    inTimeZone('Europe/Madrid');
    expect(formValuesOf(adjustment()).occurredAt).toBe('2025-03-01T00:59');
  });
});

describe('prepareSubmission: what is sent', () => {
  it('sends the five fields, the amounts as strings and the local time as UTC', () => {
    inTimeZone('UTC');
    expect(submitted(typed(), null)).toEqual({
      ok: true,
      body: {
        asset: 'BTC',
        quantity: '1.5',
        unit_cost: '20000',
        occurred_at: '2025-02-28T09:30:00.000Z',
        note: 'Opening balance.',
      },
    });
  });

  it('removes the whitespace around the asset, the quantity and the unit cost', () => {
    inTimeZone('UTC');
    const result = submitted(
      typed({ asset: '  BTC\t', quantity: ' 1.5 ', unitCost: '\n20000  ' }),
      null,
    );

    expect(result).toMatchObject({
      ok: true,
      body: { asset: 'BTC', quantity: '1.5', unit_cost: '20000' },
    });
  });

  it('sends the note exactly as typed, with the whitespace around it', () => {
    inTimeZone('UTC');
    const note = '  Bought in two lots.\n\nSecond lot in cash.  ';
    const result = submitted(typed({ note }), null);

    expect(result).toMatchObject({ ok: true, body: { note } });
  });

  it.each(['', '   ', '\t\n'])(
    'sends the empty unit cost %j as null, never as zero',
    (unitCost) => {
      inTimeZone('UTC');
      const result = submitted(typed({ unitCost }), null);

      expect(result.ok).toBe(true);
      if (result.ok) {
        expect(result.body.unit_cost).toBeNull();
        // Present and null, which a replacement requires: not left out.
        expect(Object.keys(result.body).sort()).toEqual([
          'asset',
          'note',
          'occurred_at',
          'quantity',
          'unit_cost',
        ]);
      }
    },
  );

  it('sends a unit cost of zero as "0": the owner said nothing was paid', () => {
    inTimeZone('UTC');
    const result = submitted(typed({ unitCost: '0' }), null);

    expect(result).toMatchObject({ ok: true, body: { unit_cost: '0' } });
  });

  it.each([
    ['btc', 'a lower-case symbol'],
    ['Btc', 'a mixed-case symbol'],
    ['USDT', 'a cash asset'],
    ['BTC-USD', 'a pair'],
    ['A'.repeat(21), 'a symbol of 21 characters'],
  ])("sends the asset %j as typed: %s is the server's to refuse", (asset) => {
    inTimeZone('UTC');
    const result = submitted(typed({ asset }), null);

    expect(result).toMatchObject({ ok: true, body: { asset } });
  });

  it.each([
    ['0', 'zero'],
    ['-1.5', 'a negative quantity'],
    ['1,5', 'a comma'],
    ['abc', 'letters'],
    ['0.1234567890123456789', 'nineteen places'],
    ['1e3', 'an exponent'],
    ['3.141592653589793238', 'eighteen places a double cannot hold'],
    ['00001.50', 'padding zeros'],
  ])("sends the quantity %j as typed: %s is the server's to judge", (quantity) => {
    // The form copies none of the server's rules, and it does not tidy an amount either: the
    // digits the owner typed are the digits that go.
    inTimeZone('UTC');
    const result = submitted(typed({ quantity, unitCost: quantity }), null);

    expect(result).toMatchObject({ ok: true, body: { quantity, unit_cost: quantity } });
  });

  it("sends a note of any length, and a date in the future: both are the server's rules", () => {
    inTimeZone('UTC');
    const note = 'n'.repeat(501);
    const result = submitted(typed({ note, occurredAt: '2999-03-04T05:06' }), null);

    expect(result).toMatchObject({
      ok: true,
      body: { note, occurred_at: '2999-03-04T05:06:00.000Z' },
    });
  });

  it('reads the date as local time east of Greenwich', () => {
    inTimeZone('Europe/Madrid');
    // Winter: CET, UTC+1. Half past midnight on 1 March is 23:30 UTC the day before.
    expect(submitted(typed({ occurredAt: '2025-03-01T00:30' }), null)).toMatchObject({
      body: { occurred_at: '2025-02-28T23:30:00.000Z' },
    });
    // Summer: CEST, UTC+2.
    expect(submitted(typed({ occurredAt: '2025-08-15T10:30' }), null)).toMatchObject({
      body: { occurred_at: '2025-08-15T08:30:00.000Z' },
    });
  });

  it('reads the date as local time west of Greenwich', () => {
    inTimeZone('America/New_York');
    // EST, UTC-5: 22:15 on 28 February is 03:15 UTC on 1 March.
    expect(submitted(typed({ occurredAt: '2025-02-28T22:15' }), null)).toMatchObject({
      body: { occurred_at: '2025-03-01T03:15:00.000Z' },
    });
  });

  it('reads a date with seconds, which a datetime-local input can hold', () => {
    inTimeZone('UTC');
    expect(submitted(typed({ occurredAt: '2025-02-28T09:30:15' }), null)).toMatchObject({
      body: { occurred_at: '2025-02-28T09:30:15.000Z' },
    });
  });

  it('sends the same five fields for a create and for an edit', () => {
    inTimeZone('UTC');
    const created = submitted(typed(), null);
    const edited = submitted(typed(), kasUnknownCost());

    expect(created).toEqual(edited);
  });
});

describe('prepareSubmission: the date in edit mode', () => {
  it('re-sends the stored instant byte for byte when the date is untouched', () => {
    inTimeZone('UTC');
    // Seconds and microseconds: neither fits in the field, and `Date` cannot hold the second.
    const stored = adjustment();
    const values = formValuesOf(stored);

    const result = submitted(values, stored);

    expect(values.occurredAt).toBe('2025-02-28T23:59');
    expect(result).toMatchObject({ ok: true, body: { occurred_at: BTC_OPENING_AT } });
    expect(result).not.toMatchObject({ body: { occurred_at: '2025-02-28T23:59:00.000Z' } });
    expect(result).not.toMatchObject({ body: { occurred_at: '2025-02-28T23:59:37.123Z' } });
  });

  it("re-sends it whatever the browser's zone is", () => {
    inTimeZone('Asia/Kolkata');
    const stored = adjustment();

    expect(submitted(formValuesOf(stored), stored)).toMatchObject({
      body: { occurred_at: BTC_OPENING_AT },
    });
  });

  it("re-sends an instant on a whole minute in its stored spelling, not JavaScript's", () => {
    inTimeZone('UTC');
    // Nothing is lost by a round trip here, and the bytes still must not change: the stored
    // spelling has no fractional part and `toISOString` would add one.
    const stored = ethPrecise();

    expect(submitted(formValuesOf(stored), stored)).toMatchObject({
      body: { occurred_at: '2025-08-15T08:30:00Z' },
    });
  });

  it('re-sends the stored instant when other fields changed and the date did not', () => {
    inTimeZone('UTC');
    const stored = adjustment();
    const values = { ...formValuesOf(stored), quantity: '2', unitCost: '', note: 'Corrected.' };

    expect(submitted(values, stored)).toEqual({
      ok: true,
      body: {
        asset: 'BTC',
        quantity: '2',
        unit_cost: null,
        occurred_at: BTC_OPENING_AT,
        note: 'Corrected.',
      },
    });
  });

  it('keeps an instant in the hour the clocks repeat, which the field cannot tell apart', () => {
    inTimeZone('Europe/Madrid');
    // 25 October 2026: 03:00 CEST becomes 02:00 CET, so 02:30 happens twice - at 00:30 UTC
    // and at 01:30 UTC. This adjustment is at the second one. The field shows 02:30, and a
    // round trip through the field would resolve it to the first, an hour earlier.
    const stored = adjustment({
      occurred_at: '2026-10-25T01:30:00Z',
      created_at: '2026-10-26T09:00:00.482913Z',
      updated_at: '2026-10-26T09:00:00.482913Z',
    });
    const values = formValuesOf(stored);

    expect(values.occurredAt).toBe('2026-10-25T02:30');
    expect(submitted(values, stored)).toMatchObject({
      body: { occurred_at: '2026-10-25T01:30:00Z' },
    });
  });

  it('resolves a time the owner changes to, inside the hour the clocks repeat, to its first occurrence', () => {
    // Spec 027, R12. Zone: pinned to Europe/Madrid. On 26 October 2025, 03:00 CEST becomes
    // 02:00 CET, so 02:30 happens at 00:30 UTC and again at 01:30 UTC, and a datetime-local
    // value cannot say which. The platform takes the first, and this pins that it does: the
    // owner who means the second has no way to say so, which the spec accepts.
    inTimeZone('Europe/Madrid');
    const stored = adjustment();
    const changed = { ...formValuesOf(stored), occurredAt: '2025-10-26T02:30' };

    expect(submitted(changed, stored)).toMatchObject({
      ok: true,
      body: { occurred_at: '2025-10-26T00:30:00.000Z' },
    });
    expect(submitted(typed({ occurredAt: '2025-10-26T02:30' }), null)).toMatchObject({
      ok: true,
      body: { occurred_at: '2025-10-26T00:30:00.000Z' },
    });
    // The control: the minute before the repeat and the minute after it are unambiguous.
    expect(submitted(typed({ occurredAt: '2025-10-26T01:59' }), null)).toMatchObject({
      body: { occurred_at: '2025-10-25T23:59:00.000Z' },
    });
    expect(submitted(typed({ occurredAt: '2025-10-26T03:00' }), null)).toMatchObject({
      body: { occurred_at: '2025-10-26T02:00:00.000Z' },
    });
  });

  it('sends a time the clocks skipped as an instant, and never refuses it or throws', () => {
    // Zone: pinned to Europe/Madrid. On 30 March 2025, 02:00 CET becomes 03:00 CEST, so
    // 02:30 never happens. A picker does not offer it; typed, it is a valid datetime-local
    // value, and the platform reads it with the offset from before the change: 01:30 UTC,
    // which is 03:30 on the wall. It is a `Date` like any other, so it is sent.
    inTimeZone('Europe/Madrid');

    const result = submitted(typed({ occurredAt: '2025-03-30T02:30' }), null);

    expect(result).toMatchObject({ ok: true, body: { occurred_at: '2025-03-30T01:30:00.000Z' } });
    expect(toLocalInputValue('2025-03-30T01:30:00Z')).toBe('2025-03-30T03:30');
  });

  it('sends the new local time as UTC when the date is changed', () => {
    inTimeZone('Europe/Madrid');
    const stored = adjustment();
    const values = { ...formValuesOf(stored), occurredAt: '2025-02-27T18:45' };

    // CET, UTC+1.
    expect(submitted(values, stored)).toMatchObject({
      ok: true,
      body: { occurred_at: '2025-02-27T17:45:00.000Z' },
    });
  });

  it('sends the new time when it is changed by a single minute', () => {
    inTimeZone('UTC');
    const stored = adjustment();
    const values = { ...formValuesOf(stored), occurredAt: '2025-02-28T23:58' };

    expect(submitted(values, stored)).toMatchObject({
      body: { occurred_at: '2025-02-28T23:58:00.000Z' },
    });
  });

  it('compares with what this form was mounted with, not with any other adjustment', () => {
    inTimeZone('UTC');
    // The same text in the field is the stored instant only for the form it was mounted in.
    const values = { ...formValuesOf(adjustment()), occurredAt: '2025-06-01T12:00' };

    expect(prepareSubmission(values, adjustment(), '2025-02-28T23:59')).toMatchObject({
      body: { occurred_at: '2025-06-01T12:00:00.000Z' },
    });
    expect(prepareSubmission(values, kasUnknownCost(), '2025-06-01T12:00')).toMatchObject({
      body: { occurred_at: '2025-06-01T12:00:05Z' },
    });
  });

  it('re-sends the stored instant when the zone changes between Edit and Save', () => {
    // The owner opens the edit in Madrid, where the field shows 00:59 on 1 March, and saves
    // after the browser's zone has moved to New York - a laptop that crossed an ocean, or a
    // system setting changed. They did not touch the date.
    inTimeZone('Europe/Madrid');
    const stored = adjustment();
    const values = formValuesOf(stored);
    const mounted = values.occurredAt;
    expect(mounted).toBe('2025-03-01T00:59');

    inTimeZone('America/New_York');
    // The control: worked out again now, the field would be expected to hold something else,
    // which is how an untouched date would be taken for a changed one.
    expect(toLocalInputValue(stored.occurred_at)).toBe('2025-02-28T18:59');

    const result = prepareSubmission({ ...values, note: 'Corrected.' }, stored, mounted);

    expect(result).toMatchObject({ ok: true, body: { occurred_at: BTC_OPENING_AT } });
    // Not the field's text read in the new zone, five hours behind UTC.
    expect(result).not.toMatchObject({ body: { occurred_at: '2025-03-01T05:59:00.000Z' } });
  });

  it('re-sends it across a zone change in the other direction too', () => {
    inTimeZone('America/New_York');
    const stored = adjustment();
    const values = formValuesOf(stored);
    expect(values.occurredAt).toBe('2025-02-28T18:59');

    inTimeZone('Asia/Kolkata');

    expect(prepareSubmission(values, stored, values.occurredAt)).toMatchObject({
      body: { occurred_at: BTC_OPENING_AT },
    });
  });

  it('takes a date the owner did change after a zone change as the local time it now is', () => {
    inTimeZone('Europe/Madrid');
    const stored = adjustment();
    const mounted = formValuesOf(stored).occurredAt;

    inTimeZone('America/New_York');
    // What the field would have shown had the edit been opened here. It is not what this form
    // was mounted with, so it is the owner's own entry: 18:59 EST, and no seconds.
    const values = { ...formValuesOf(stored), occurredAt: '2025-02-28T18:59' };

    expect(prepareSubmission(values, stored, mounted)).toMatchObject({
      body: { occurred_at: '2025-02-28T23:59:00.000Z' },
    });
  });

  it('judges "untouched" by what the field holds: changed and changed back is untouched', () => {
    inTimeZone('UTC');
    const stored = adjustment();
    const mounted = formValuesOf(stored).occurredAt;
    const changed = { ...formValuesOf(stored), occurredAt: '2025-01-01T00:00' };
    const back = { ...changed, occurredAt: mounted };

    expect(prepareSubmission(changed, stored, mounted)).toMatchObject({
      body: { occurred_at: '2025-01-01T00:00:00.000Z' },
    });
    expect(prepareSubmission(back, stored, mounted)).toMatchObject({
      body: { occurred_at: BTC_OPENING_AT },
    });
  });

  it('ignores the mounted value in create mode: there is no stored instant to send', () => {
    inTimeZone('UTC');
    const values = typed({ occurredAt: '2025-02-28T23:59' });

    expect(prepareSubmission(values, null, '2025-02-28T23:59')).toMatchObject({
      body: { occurred_at: '2025-02-28T23:59:00.000Z' },
    });
    expect(prepareSubmission(values, null, '')).toMatchObject({
      body: { occurred_at: '2025-02-28T23:59:00.000Z' },
    });
  });

  it('has no stored instant to re-send in create mode', () => {
    inTimeZone('UTC');
    expect(submitted(typed({ occurredAt: '2025-02-28T23:59' }), null)).toMatchObject({
      body: { occurred_at: '2025-02-28T23:59:00.000Z' },
    });
  });
});

describe('prepareSubmission: the local refusals', () => {
  it.each(['', '   ', '\t\n'])('refuses the asset %j', (asset) => {
    expect(submitted(typed({ asset }), null)).toEqual({
      ok: false,
      errors: { asset: ASSET_REQUIRED },
    });
  });

  it.each(['', '   ', '\t\n'])('refuses the quantity %j', (quantity) => {
    expect(submitted(typed({ quantity }), null)).toEqual({
      ok: false,
      errors: { quantity: QUANTITY_REQUIRED },
    });
  });

  it.each(['', '   ', '\n\n'])('refuses the note %j', (note) => {
    expect(submitted(typed({ note }), null)).toEqual({
      ok: false,
      errors: { note: NOTE_REQUIRED },
    });
  });

  it('refuses an empty date', () => {
    expect(submitted(typed({ occurredAt: '' }), null)).toEqual({
      ok: false,
      errors: { occurredAt: DATE_REQUIRED },
    });
  });

  it.each([
    ['275760-09-13T00:01', 'a six-digit year, which a datetime-local input accepts'],
    ['10000-01-01T00:00', 'the year after 9999'],
    ['not a date', 'text that is not a date at all'],
  ])('refuses the date %j: %s', (occurredAt) => {
    // `new Date(value)` is an invalid `Date` for these, and `toISOString` throws on one.
    expect(submitted(typed({ occurredAt }), null)).toEqual({
      ok: false,
      errors: { occurredAt: DATE_UNREPRESENTABLE },
    });
  });

  it('refuses an empty or unrepresentable date in edit mode as well', () => {
    const stored = adjustment();

    expect(submitted({ ...formValuesOf(stored), occurredAt: '' }, stored)).toEqual({
      ok: false,
      errors: { occurredAt: DATE_REQUIRED },
    });
    expect(
      submitted({ ...formValuesOf(stored), occurredAt: '275760-09-13T00:01' }, stored),
    ).toEqual({ ok: false, errors: { occurredAt: DATE_UNREPRESENTABLE } });
  });

  it('never refuses an empty unit cost', () => {
    inTimeZone('UTC');
    expect(submitted(typed({ unitCost: '' }), null).ok).toBe(true);
  });

  it('reports every refusal at once, each under its own field', () => {
    expect(submitted(emptyFormValues(''), null)).toEqual({
      ok: false,
      errors: {
        asset: ASSET_REQUIRED,
        quantity: QUANTITY_REQUIRED,
        occurredAt: DATE_REQUIRED,
        note: NOTE_REQUIRED,
      },
    });
  });

  it('refuses a date alone, beside fields that are fine', () => {
    expect(submitted(typed({ note: ' ', occurredAt: 'not a date' }), null)).toEqual({
      ok: false,
      errors: { note: NOTE_REQUIRED, occurredAt: DATE_UNREPRESENTABLE },
    });
  });

  it('says what is required in a sentence', () => {
    expect(ASSET_REQUIRED).toBe('An asset is required.');
    expect(QUANTITY_REQUIRED).toBe('A quantity is required.');
    expect(NOTE_REQUIRED).toBe('A note is required.');
    expect(DATE_REQUIRED).toBe('A date and time are required.');
    expect(DATE_UNREPRESENTABLE).toBe('This date is outside the range your browser can represent.');
  });
});

describe('mapAdjustmentError', () => {
  it.each([
    ['asset', 'asset', ASSET_SYMBOL_RULE],
    ['asset', 'asset', CASH_ASSET_RULE],
    ['quantity', 'quantity', QUANTITY_NOT_POSITIVE_RULE],
    ['unit_cost', 'unitCost', UNIT_COST_NEGATIVE_RULE],
    ['unit_cost', 'unitCost', TOTAL_COST_TOO_LARGE_RULE],
    ['occurred_at', 'occurredAt', OCCURRED_IN_FUTURE_RULE],
    ['note', 'note', NOTE_BLANK_RULE],
    ['note', 'note', NOTE_TOO_LONG_RULE],
  ])("shows a 422 at ['body', %j] under %s, in the server's own sentence", (wire, field, rule) => {
    expect(mapAdjustmentError(validationError([body(wire, rule)]))).toEqual({ [field]: rule });
  });

  it('maps by the last element of loc, wherever the field sits', () => {
    expect(
      mapAdjustmentError(validationError([{ loc: ['unit_cost'], msg: UNIT_COST_NEGATIVE_RULE }])),
    ).toEqual({ unitCost: UNIT_COST_NEGATIVE_RULE });
  });

  it('shows several refusals at once, each under its own field', () => {
    expect(
      mapAdjustmentError(
        validationError([
          body('asset', 'Field required'),
          body('quantity', 'Input should be a valid decimal'),
          body('unit_cost', 'Input should be a valid decimal'),
          body('occurred_at', 'Field required'),
          body('note', 'Field required'),
        ]),
      ),
    ).toEqual({
      asset: 'Field required',
      quantity: 'Input should be a valid decimal',
      unitCost: 'Input should be a valid decimal',
      occurredAt: 'Field required',
      note: 'Field required',
    });
  });

  it.each([
    [['body'], 'Input should be a valid dictionary'],
    [['body', 'colour'], 'Extra inputs are not permitted'],
    [['path', 'adjustment_id'], 'Input should be greater than or equal to 1'],
    [['query', 'dry_run'], 'Input should be a valid boolean'],
    [[], 'A refusal with no location at all'],
    // The wire's name is `unit_cost`; the form's own name for it is not a location.
    [['body', 'unitCost'], 'Not a field of the request'],
    [['body', 'occurredAt'], 'Not a field of the request'],
    [['body', 'ASSET'], 'Not a field of the request'],
  ])('shows an entry at %j at the bottom of the form', (loc, msg) => {
    expect(mapAdjustmentError(validationError([{ loc, msg }]))).toEqual({ form: [msg] });
  });

  it('keeps the field messages beside the ones that belong to no field', () => {
    expect(
      mapAdjustmentError(
        validationError([
          body('quantity', QUANTITY_NOT_POSITIVE_RULE),
          { loc: ['body', 'colour'], msg: 'Extra inputs are not permitted' },
          body('note', NOTE_BLANK_RULE),
          { loc: ['body'], msg: 'Something else' },
        ]),
      ),
    ).toEqual({
      quantity: QUANTITY_NOT_POSITIVE_RULE,
      note: NOTE_BLANK_RULE,
      form: ['Extra inputs are not permitted', 'Something else'],
    });
  });

  it('has no form entry when every message found its field', () => {
    const mapped = mapAdjustmentError(validationError([body('asset', ASSET_SYMBOL_RULE)]));

    expect('form' in mapped).toBe(false);
  });

  it("shows a 422 with no readable errors as the API's detail, at the bottom", () => {
    expect(mapAdjustmentError(validationError([]))).toEqual({ form: [VALIDATION_DETAIL] });
    expect(
      mapAdjustmentError(
        problemError({ status: 422, title: 'Unprocessable Entity', detail: VALIDATION_DETAIL }),
      ),
    ).toEqual({ form: [VALIDATION_DETAIL] });
  });

  it("shows a 404 as the API's detail, at the bottom of the form", () => {
    expect(
      mapAdjustmentError(
        problemError({ status: 404, title: 'Not Found', detail: ADJUSTMENT_NOT_FOUND_DETAIL }),
      ),
    ).toEqual({ form: ['No adjustment with that id.'] });
  });

  it.each([
    [500, 'Internal Server Error', 'The server encountered an unexpected condition.'],
    [503, 'Service Unavailable', 'The database is not reachable.'],
    [403, 'Forbidden', 'A state-changing request must declare a JSON body.'],
  ])("shows a %i as the API's detail, at the bottom of the form", (status, title, detail) => {
    expect(mapAdjustmentError(problemError({ status, title, detail }))).toEqual({ form: [detail] });
  });

  it("shows the problem's title when it has no detail", () => {
    expect(
      mapAdjustmentError(problemError({ status: 500, title: 'Internal Server Error' })),
    ).toEqual({ form: ['Internal Server Error'] });
  });

  it('says the save failed, in words, when the server never answered', () => {
    expect(mapAdjustmentError(new TypeError('Failed to fetch'))).toEqual({ form: [SAVE_FAILURE] });
    expect(mapAdjustmentError(undefined)).toEqual({ form: [SAVE_FAILURE] });
    expect(SAVE_FAILURE).toBe(
      'Could not save the adjustment. Check your connection and try again.',
    );
  });

  it('says the save failed when a proxy answered instead of the backend, not its reason phrase', () => {
    // A 502 with an HTML body: `api/client.ts` synthesises the problem from the status line.
    const proxied = new ApiError({ type: 'about:blank', title: 'Bad Gateway', status: 502 }, false);

    expect(mapAdjustmentError(proxied)).toEqual({ form: [SAVE_FAILURE] });
  });
});

describe('firstTradeOf', () => {
  it('finds the first trade of an asset the history holds', () => {
    expect(firstTradeOf(threeFirstTrades(), 'BTC')).toEqual({
      asset: 'BTC',
      first_trade_at: BTC_FIRST_TRADE_AT,
    });
    expect(firstTradeOf(threeFirstTrades(), 'KAS')?.first_trade_at).toBe('2025-11-02T05:30:00Z');
  });

  it('ignores the whitespace around what was typed, as the request does', () => {
    expect(firstTradeOf(threeFirstTrades(), '  BTC\t')?.asset).toBe('BTC');
  });

  it.each(['btc', 'Btc', 'bTC'])(
    'does not match %j: the symbol is exact, case included',
    (typed) => {
      // A lower-case symbol is one the server refuses: a hint for it would be a hint for an
      // adjustment that cannot be recorded.
      expect(firstTradeOf(threeFirstTrades(), typed)).toBeUndefined();
    },
  );

  it.each(['BT', 'BTCB', 'B TC', 'BTC,ETH', '', '   ', 'DOGE'])(
    'does not match %j: a prefix, a longer symbol or another asset is not the asset',
    (typed) => {
      expect(firstTradeOf(threeFirstTrades(), typed)).toBeUndefined();
    },
  );

  it('matches a symbol that differs only in case from another, each to its own', () => {
    const trades = firstTrades([
      firstTrade('BTC', BTC_FIRST_TRADE_AT),
      firstTrade('btc', '2025-11-02T05:30:00Z'),
    ]);

    expect(firstTradeOf(trades, 'BTC')?.first_trade_at).toBe(BTC_FIRST_TRADE_AT);
    expect(firstTradeOf(trades, 'btc')?.first_trade_at).toBe('2025-11-02T05:30:00Z');
  });

  it('finds nothing while first-trades is pending or has failed', () => {
    expect(firstTradeOf(undefined, 'BTC')).toBeUndefined();
  });

  it('finds nothing for an owner with no fills', () => {
    expect(firstTradeOf(firstTrades(), 'BTC')).toBeUndefined();
  });
});

describe('suggestedDate', () => {
  it('is local midnight at the start of the day before the trade', () => {
    inTimeZone('UTC');
    // The trade is on 12 June at 10:00:37; the day before is 11 June.
    const suggestion = suggestedDate('2025-06-12T10:00:37Z');

    expect(suggestion.value).toBe('2025-06-11T00:00');
    expect(plain(suggestion.label)).toBe('Jun 11, 2025, 12:00 AM');
  });

  it('crosses a month boundary: the day before 1 March is 28 February', () => {
    inTimeZone('UTC');
    const suggestion = suggestedDate(BTC_FIRST_TRADE_AT);

    expect(suggestion.value).toBe('2025-02-28T00:00');
    expect(plain(suggestion.label)).toBe('Feb 28, 2025, 12:00 AM');
  });

  it('crosses a month boundary into a leap day', () => {
    inTimeZone('UTC');
    expect(suggestedDate('2028-03-01T10:00:37Z').value).toBe('2028-02-29T00:00');
  });

  it('crosses a year boundary: the day before 1 January is 31 December', () => {
    inTimeZone('UTC');
    const suggestion = suggestedDate('2026-01-01T05:00:00Z');

    expect(suggestion.value).toBe('2025-12-31T00:00');
    expect(plain(suggestion.label)).toBe('Dec 31, 2025, 12:00 AM');
  });

  it.each([
    ['2025-05-01T00:00:00Z', '2025-04-30T00:00'],
    ['2025-08-01T12:00:00Z', '2025-07-31T00:00'],
    ['2025-12-01T23:59:59Z', '2025-11-30T00:00'],
  ])('ends on the last day of the month before %s, whatever its length', (trade, value) => {
    inTimeZone('UTC');
    expect(suggestedDate(trade).value).toBe(value);
  });

  it('is a whole day before a trade in the first second of its day, and in the last', () => {
    inTimeZone('UTC');
    // Not "24 hours before the trade", which would be a different time of day for each.
    expect(suggestedDate('2025-06-12T00:00:00Z').value).toBe('2025-06-11T00:00');
    expect(suggestedDate('2025-06-12T00:00:00.000001Z').value).toBe('2025-06-11T00:00');
    expect(suggestedDate('2025-06-12T23:59:59.999999Z').value).toBe('2025-06-11T00:00');
  });

  it("counts the day in the browser's zone, east of Greenwich", () => {
    inTimeZone('Pacific/Kiritimati');
    // UTC+14: 10:00:37 UTC on 1 March is 00:00:37 on 2 March here, so the day before is
    // 1 March - a day later than the UTC answer.
    const suggestion = suggestedDate(BTC_FIRST_TRADE_AT);

    expect(suggestion.value).toBe('2025-03-01T00:00');
    expect(plain(suggestion.label)).toBe('Mar 1, 2025, 12:00 AM');
  });

  it("counts the day in the browser's zone, west of Greenwich", () => {
    inTimeZone('Pacific/Pago_Pago');
    // UTC-11: 10:00:37 UTC on 1 March is 23:00:37 on 28 February here, so the day before is
    // 27 February - a day earlier than the UTC answer.
    const suggestion = suggestedDate(BTC_FIRST_TRADE_AT);

    expect(suggestion.value).toBe('2025-02-27T00:00');
    expect(plain(suggestion.label)).toBe('Feb 27, 2025, 12:00 AM');
  });

  it('is midnight, not 23:00, when the day before is the day the clocks go forward', () => {
    inTimeZone('Europe/Madrid');
    // 29 March 2026: 02:00 CET becomes 03:00 CEST, so the day is 23 hours long. The trade is
    // on 30 March at 10:00 CEST. "The trade's midnight minus 24 hours" is 23:00 on 28 March.
    const suggestion = suggestedDate('2026-03-30T08:00:00Z');

    expect(suggestion.value).toBe('2026-03-29T00:00');
    expect(plain(suggestion.label)).toBe('Mar 29, 2026, 12:00 AM');
    // And the instant the form then sends is that midnight, at UTC+1.
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2026-03-28T23:00:00.000Z' },
    });
  });

  it('is midnight, not 01:00, when the day before is the day the clocks go back', () => {
    inTimeZone('Europe/Madrid');
    // 25 October 2026: 03:00 CEST becomes 02:00 CET, so the day is 25 hours long. The trade
    // is on 26 October at 10:00 CET. "The trade's midnight minus 24 hours" is 01:00 on the 25th.
    const suggestion = suggestedDate('2026-10-26T09:00:00Z');

    expect(suggestion.value).toBe('2026-10-25T00:00');
    expect(plain(suggestion.label)).toBe('Oct 25, 2026, 12:00 AM');
    // That midnight is still in summer time, at UTC+2.
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2026-10-24T22:00:00.000Z' },
    });
  });

  it('is the day before a trade made on the day the clocks change', () => {
    inTimeZone('America/New_York');
    // 8 March 2026, EST to EDT at 02:00. A trade at 12:00 EDT that day: the day before is
    // 7 March, whose midnight is at UTC-5.
    const suggestion = suggestedDate('2026-03-08T16:00:00Z');

    expect(suggestion.value).toBe('2026-03-07T00:00');
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2026-03-07T05:00:00.000Z' },
    });
  });

  it('crosses a month boundary and a clock change at once', () => {
    inTimeZone('Europe/Madrid');
    // In 2024 the clocks went forward on 31 March, the last day of the month. The trade is on
    // 1 April at 10:00 CEST; the day before is 31 March, 23 hours long, whose midnight is at
    // UTC+1. "The trade's midnight minus 24 hours" is 23:00 on 30 March: the wrong day.
    const suggestion = suggestedDate('2024-04-01T08:00:00Z');

    expect(suggestion.value).toBe('2024-03-31T00:00');
    expect(plain(suggestion.label)).toBe('Mar 31, 2024, 12:00 AM');
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2024-03-30T23:00:00.000Z' },
    });
  });

  it('crosses a month boundary into the day the clocks go back', () => {
    inTimeZone('America/New_York');
    // In 2026 the clocks go back on 1 November (EDT, UTC-4, to EST, UTC-5). The trade is on
    // 2 November at 10:00 EST; the day before is 1 November, 25 hours long, whose midnight is
    // still at UTC-4.
    const suggestion = suggestedDate('2026-11-02T15:00:00Z');

    expect(suggestion.value).toBe('2026-11-01T00:00');
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2026-11-01T04:00:00.000Z' },
    });
    // And from that day, back over the month boundary.
    expect(suggestedDate('2026-11-01T17:00:00Z').value).toBe('2026-10-31T00:00');
  });

  it('is the first instant of a day that has no midnight', () => {
    inTimeZone('America/Santiago');
    // Santiago's clocks change at midnight: 6 September 2026 jumps from 00:00 to 01:00, so
    // that day starts at 01:00. The trade is on 7 September at 10:00 (UTC-3).
    const suggestion = suggestedDate('2026-09-07T13:00:00Z');

    expect(suggestion.value).toBe('2026-09-06T01:00');
    expect(plain(suggestion.label)).toBe('Sep 6, 2026, 1:00 AM');
    // The value and the instant agree: 01:00 at UTC-3 is 04:00 UTC, the start of the day.
    expect(submitted(typed({ occurredAt: suggestion.value }), null)).toMatchObject({
      body: { occurred_at: '2026-09-06T04:00:00.000Z' },
    });
  });

  it('is always before the trade it is suggested for, in every zone tried', () => {
    // The point of the suggestion: an opening balance dated at it replays before the first
    // imported trade. Checked on the instants, which do not depend on how they are spelled.
    inTimeZone('UTC');
    for (const zone of [
      'UTC',
      'Europe/Madrid',
      'America/New_York',
      'America/Santiago',
      'Australia/Lord_Howe',
      'Pacific/Kiritimati',
      'Pacific/Pago_Pago',
      'Asia/Kolkata',
    ]) {
      // `inTimeZone` above has registered the way back to the machine's own zone.
      process.env.TZ = zone;
      for (const trade of [
        BTC_FIRST_TRADE_AT,
        '2026-03-29T00:30:00Z',
        '2026-03-30T08:00:00Z',
        '2026-10-25T23:30:00Z',
        '2026-10-26T09:00:00Z',
        '2026-09-06T04:00:00Z',
        '2026-01-01T00:00:00Z',
      ]) {
        const { value } = suggestedDate(trade);
        const gap = Date.parse(trade) - new Date(value).getTime();
        // The start of the day before: at least the 23 hours the shortest day has, and less
        // than the 49 a day of 25 hours and the whole day after it have.
        expect(gap, `${zone} ${trade}`).toBeGreaterThanOrEqual(23 * 3_600_000);
        expect(gap, `${zone} ${trade}`).toBeLessThan(49 * 3_600_000);
        expect(value, `${zone} ${trade}`).toMatch(/^\d{4}-\d{2}-\d{2}T0[01]:[03]0$/);
      }
    }
  });
});

describe('adjustmentName', () => {
  it('names an adjustment by its asset and when it was acquired, in local time', () => {
    inTimeZone('UTC');
    expect(plain(adjustmentName(adjustment()))).toBe('BTC acquired Feb 28, 2025, 11:59 PM');
    expect(plain(adjustmentName(kasUnknownCost()))).toBe('KAS acquired Jun 1, 2025, 12:00 PM');
  });

  it('tells two adjustments of one asset apart by their dates', () => {
    inTimeZone('UTC');
    const first = adjustment();
    const second = adjustment({ id: 9, occurred_at: '2025-03-15T08:00:00Z' });

    expect(adjustmentName(first)).not.toBe(adjustmentName(second));
  });

  it("follows the browser's zone", () => {
    inTimeZone('Europe/Madrid');
    expect(plain(adjustmentName(adjustment()))).toBe('BTC acquired Mar 1, 2025, 12:59 AM');
  });
});
