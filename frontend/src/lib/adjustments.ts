/**
 * The manual adjustments page's logic that is not React: what the form refuses, what it sends,
 * how a failed save becomes messages under the fields, how a stored instant becomes what a
 * date input holds, and the date the form suggests. See
 * docs/specs/027-manual-adjustments-page.md.
 *
 * No React anywhere in this module - it is exercised directly by tests and by the components
 * under `src/pages/adjustments/`, the same split `lib/fillFilters.ts` and `lib/exchanges.ts`
 * use.
 *
 * **The server is the validator** (spec 023). The only things refused here are the ones the
 * form cannot send at all: a blank asset, quantity or note, and a date that is empty or that
 * the browser's clock cannot represent. A symbol's spelling, an amount's scale, a note's
 * length and a date in the future are the server's to refuse, with its own sentence, and this
 * module holds no copy of any of those rules.
 */
import type {
  Adjustment,
  AdjustmentReplaceRequest,
  FirstTrades,
  FirstTrade,
} from '@/api/adjustments';
import { ApiError, describeApiError } from '@/api/client';
import { money, plainMoney } from '@/lib/money';
import { formatAbsoluteTime, parseInstant } from '@/lib/time';

/** The page's route, which the holdings check also links to. */
export const ADJUSTMENTS_ROUTE = '/adjustments';

/**
 * The page's route with `asset` in its query, for the link beside an asset whose balances
 * exceed its history. Built with `URLSearchParams`, never by concatenation: the symbol is
 * data, and one that held a `&` or a `#` would otherwise change what the link means.
 */
export function adjustmentsRouteFor(asset: string): string {
  return `${ADJUSTMENTS_ROUTE}?${new URLSearchParams({ asset }).toString()}`;
}

/** The form's five fields as the inputs hold them: text, exactly as typed. */
export interface AdjustmentFormValues {
  readonly asset: string;
  readonly quantity: string;
  /** Empty means the cost is unknown, which is sent as `null` and is not zero. */
  readonly unitCost: string;
  /** A `datetime-local` value, `YYYY-MM-DDTHH:mm` in the browser's local time, or empty. */
  readonly occurredAt: string;
  readonly note: string;
}

/** An empty create form, with `asset` filled when the URL carried one. */
export function emptyFormValues(asset: string): AdjustmentFormValues {
  return { asset, quantity: '', unitCost: '', occurredAt: '', note: '' };
}

/**
 * The form filled from a stored adjustment, for edit mode. The amounts are spelled with their
 * trailing zeros removed (`1.5`, not `1.500000000000000000`), which `plainMoney` does exactly,
 * so they stay strings from the first byte to the last. A `null` cost is an empty field.
 */
export function formValuesOf(adjustment: Adjustment): AdjustmentFormValues {
  return {
    asset: adjustment.asset,
    quantity: plainMoney(money(adjustment.quantity)),
    unitCost: adjustment.unit_cost === null ? '' : plainMoney(money(adjustment.unit_cost)),
    occurredAt: toLocalInputValue(adjustment.occurred_at),
    note: adjustment.note,
  };
}

function pad(value: number, width: number): string {
  return String(value).padStart(width, '0');
}

function localInputValueOf(date: Date): string {
  return (
    `${pad(date.getFullYear(), 4)}-${pad(date.getMonth() + 1, 2)}-${pad(date.getDate(), 2)}` +
    `T${pad(date.getHours(), 2)}:${pad(date.getMinutes(), 2)}`
  );
}

/**
 * What a `datetime-local` input holds for the instant `iso`: its local date and time to the
 * minute. A stored instant can carry seconds the input cannot show, which is why edit mode
 * compares the field with the value it was mounted with, to tell whether the owner touched the
 * date at all, and sends the stored instant itself when they did not.
 */
export function toLocalInputValue(iso: string): string {
  return localInputValueOf(new Date(parseInstant(iso)));
}

/** The fields an error can be shown under. `form` is the bottom of the form, below them all. */
export interface FieldErrors {
  readonly asset?: string;
  readonly quantity?: string;
  readonly unitCost?: string;
  readonly occurredAt?: string;
  readonly note?: string;
  /** Messages that belong to no field: shown at the bottom of the form. */
  readonly form?: readonly string[];
}

export const NO_ERRORS: FieldErrors = {};

export const ASSET_REQUIRED = 'An asset is required.';
export const QUANTITY_REQUIRED = 'A quantity is required.';
export const NOTE_REQUIRED = 'A note is required.';
export const DATE_REQUIRED = 'A date and time are required.';
export const DATE_UNREPRESENTABLE = 'This date is outside the range your browser can represent.';
export const SAVE_FAILURE = 'Could not save the adjustment. Check your connection and try again.';

type LocalField = 'asset' | 'quantity' | 'occurredAt' | 'note';

/**
 * The instant to send, or why there is none. An untouched date in edit mode is the stored
 * instant itself, byte for byte: the field shows it to the minute, and re-sending a rounded
 * one would move the adjustment among fills of the same minute without the owner having
 * asked. A changed date is the local time as a UTC instant.
 *
 * "Untouched" is the field holding the string the form was mounted with, `mountedLocal`, and
 * not one worked out again now: if the browser's time zone changed between Edit and Save, a
 * recomputed value would differ and an untouched date would move. It is a comparison by
 * value, so changing the field and changing it back still sends the stored instant. A time
 * the owner changes to inside the hour that repeats when the clocks go back resolves to its
 * first occurrence.
 *
 * `new Date(value)` is an invalid `Date` for a value the clock cannot represent - the input
 * accepts a year of up to six digits, and the date-time syntax `Date` reads has four - and
 * `toISOString` throws on one, so it is refused here instead of reaching it.
 */
function instantOf(
  local: string,
  editing: Adjustment | null,
  mountedLocal: string,
): { readonly instant: string } | { readonly refusal: string } {
  if (local === '') {
    return { refusal: DATE_REQUIRED };
  }
  if (editing !== null && local === mountedLocal) {
    return { instant: editing.occurred_at };
  }

  const date = new Date(local);
  return Number.isNaN(date.getTime())
    ? { refusal: DATE_UNREPRESENTABLE }
    : { instant: date.toISOString() };
}

export type Submission =
  | { readonly ok: true; readonly body: AdjustmentReplaceRequest }
  | { readonly ok: false; readonly errors: FieldErrors };

/**
 * What the form sends, or what it refuses to send.
 *
 * - `asset`, `quantity` and `unit_cost` go as typed with the whitespace around them removed.
 *   Nothing else is changed: a lower-case symbol goes lower-case and the server refuses it.
 *   An empty cost is `null`, an unknown cost, which is not zero.
 * - `note` goes exactly as typed.
 * - A create and an edit send the same five fields. `unit_cost` is always present, because a
 *   replacement has to state it and a create may.
 *
 * A blank asset, quantity or note, and a date that is empty or unrepresentable, are refused
 * with no request sent, each under its field.
 *
 * `mountedOccurredAt` is the date field's value when the form was mounted, which edit mode
 * compares the field against; see {@link instantOf}. A create form has no stored instant and
 * ignores it.
 */
export function prepareSubmission(
  values: AdjustmentFormValues,
  editing: Adjustment | null,
  mountedOccurredAt: string,
): Submission {
  const asset = values.asset.trim();
  const quantity = values.quantity.trim();
  const unitCost = values.unitCost.trim();
  const when = instantOf(values.occurredAt, editing, mountedOccurredAt);

  const errors: Partial<Record<LocalField, string>> = {};
  if (asset === '') {
    errors.asset = ASSET_REQUIRED;
  }
  if (quantity === '') {
    errors.quantity = QUANTITY_REQUIRED;
  }
  if (values.note.trim() === '') {
    errors.note = NOTE_REQUIRED;
  }

  if ('instant' in when && Object.keys(errors).length === 0) {
    return {
      ok: true,
      body: {
        asset,
        quantity,
        unit_cost: unitCost === '' ? null : unitCost,
        occurred_at: when.instant,
        note: values.note,
      },
    };
  }

  if ('refusal' in when) {
    errors.occurredAt = when.refusal;
  }
  return { ok: false, errors };
}

type FieldKey = 'asset' | 'quantity' | 'unitCost' | 'occurredAt' | 'note';

/** The input a 422's `loc` names, by its last element: `["body", "unit_cost"]` is `unitCost`. */
function fieldOf(loc: readonly string[]): FieldKey | undefined {
  switch (loc.at(-1)) {
    case 'asset':
      return 'asset';
    case 'quantity':
      return 'quantity';
    case 'unit_cost':
      return 'unitCost';
    case 'occurred_at':
      return 'occurredAt';
    case 'note':
      return 'note';
    default:
      return undefined;
  }
}

/**
 * Maps a failed save onto messages under the fields. A 422's `errors` are mapped by the last
 * element of `loc`, each message the server's own sentence; an entry at any other location
 * joins `form`, shown at the bottom, and so does any other failure - a 404 for an adjustment
 * deleted elsewhere, a 5xx, a network error - with the API's own detail when it has one.
 */
export function mapAdjustmentError(error: unknown): FieldErrors {
  if (error instanceof ApiError) {
    const entries = error.problem.errors;
    if (entries !== undefined && entries.length > 0) {
      const fields: Partial<Record<FieldKey, string>> = {};
      const form: string[] = [];

      for (const entry of entries) {
        const field = fieldOf(entry.loc);
        if (field === undefined) {
          form.push(entry.msg);
        } else {
          fields[field] = entry.msg;
        }
      }

      return form.length > 0 ? { ...fields, form } : fields;
    }
  }

  return { form: [describeApiError(error, SAVE_FAILURE)] };
}

/** The first of `trades` whose asset is `asset`, trimmed, exactly: `btc` is not `BTC`. */
export function firstTradeOf(
  trades: FirstTrades | undefined,
  asset: string,
): FirstTrade | undefined {
  const symbol = asset.trim();
  return trades?.assets.find((trade) => trade.asset === symbol);
}

/** A date the form can set its date field to, and how the button that sets it reads. */
export interface SuggestedDate {
  /** A `datetime-local` value. */
  readonly value: string;
  /** The same moment as the owner reads it: "Feb 28, 2025, 12:00 AM". */
  readonly label: string;
}

/**
 * Local midnight at the start of the day **before** the local day of `firstTradeAt`.
 *
 * Found with calendar arithmetic, from the trade's own local year, month and date: moving the
 * date back by one on a fresh local midnight leaves the platform to resolve that local time,
 * which is right across a month boundary (day 0 is the last day of the month before) and on
 * the two days a year the clocks change, where `midnight - 24 h` is not. Where the clocks jump
 * *at* midnight, the platform resolves the missing 00:00 to the first instant the day has, and
 * `value` and `label` both say so. `setFullYear` also keeps a year below 100 as written, where
 * `new Date(y, m, d)` reads it as 19xx. See `lib/fillFilters.ts` for the same arithmetic going
 * forward.
 */
export function suggestedDate(firstTradeAt: string): SuggestedDate {
  const trade = new Date(parseInstant(firstTradeAt));
  const day = new Date(2000, 0, 1);
  day.setFullYear(trade.getFullYear(), trade.getMonth(), trade.getDate() - 1);

  return { value: localInputValueOf(day), label: formatAbsoluteTime(day.toISOString()) };
}

/**
 * What identifies an adjustment to a screen reader: its asset and when it was acquired.
 * Folded into every per-row control's accessible name, because every row's "Edit" and
 * "Delete" would otherwise share one name across the whole list.
 */
export function adjustmentName(adjustment: Adjustment): string {
  return `${adjustment.asset} acquired ${formatAbsoluteTime(adjustment.occurred_at)}`;
}
