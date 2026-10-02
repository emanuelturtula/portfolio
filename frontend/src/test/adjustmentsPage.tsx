import type { QueryClient } from '@tanstack/react-query';
import { fireEvent, screen, within } from '@testing-library/react';
import userEvent, { type UserEvent } from '@testing-library/user-event';
import type { ReactNode } from 'react';

import type { PositionsResponse } from './accountingFixtures';
import { emptySnapshot } from './accountingFixtures';
import {
  threeAdjustments,
  threeFirstTrades,
  type AdjustmentResponse,
  type FirstTradesResponse,
} from './adjustmentFixtures';
import { fakeAccounting, type FakeAccounting } from './fakeAccounting';
import { fakeAdjustments, type FakeAdjustments } from './fakeAdjustments';
import { renderApp } from './render';
import { fakeSession, server, TEST_USERNAME } from './server';

/**
 * What the tests of the manual adjustments page (spec 027) share: how the page is opened, and
 * how its form, its list and its messages are found.
 *
 * Everything is found the way the owner finds it - by a label, a role, an accessible name, a
 * column heading - and never by an id or a class the implementation happens to use. An error
 * "under its field" is the alert the input is described by, read through `aria-describedby`,
 * because that relation is what the spec requires and what a screen reader follows.
 */

export interface PageFakes {
  readonly adjustments: FakeAdjustments;
  readonly accounting: FakeAccounting;
}

export interface OpenOptions {
  /** Where the app is opened. Defaults to `/adjustments`. */
  readonly path?: string;
  /** The owner's adjustments. Defaults to BTC, KAS (unknown cost) and ETH (eighteen places). */
  readonly adjustments?: readonly AdjustmentResponse[];
  /** Defaults to BTC, ETH and KAS, each with the instant its imported history begins. */
  readonly firstTrades?: FirstTradesResponse;
  /** The snapshot the page reads for `last_recompute`. Defaults to a written, empty one. */
  readonly positions?: PositionsResponse;
  /** Run before the first render, so a fake can be held or failed from the start. */
  readonly before?: (fakes: PageFakes) => void;
  /**
   * Run after a create, a replace or a delete has changed the stored adjustments and before
   * it is answered: where the backend recomputes the positions.
   */
  readonly onChange?: (fakes: PageFakes) => void;
  /** Rendered beside the app, inside the same providers: a probe of the test's own. */
  readonly extra?: ReactNode;
}

export interface PageSetup extends PageFakes {
  readonly user: UserEvent;
  /** The shipped query client the page runs on, for a refetch the page did not ask for. */
  readonly queryClient: QueryClient;
}

/** Signs in, installs the fakes of every endpoint the page reads, and opens the page. */
export function openAdjustmentsPage(options: OpenOptions = {}): PageSetup {
  const user = userEvent.setup();
  const accounting = fakeAccounting({ positions: options.positions ?? emptySnapshot() });
  const adjustments: FakeAdjustments = fakeAdjustments({
    adjustments: options.adjustments ?? threeAdjustments(),
    firstTrades: options.firstTrades ?? threeFirstTrades(),
    onChange: () => {
      options.onChange?.({ adjustments, accounting });
    },
  });
  options.before?.({ adjustments, accounting });
  server.use(
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...accounting.handlers,
    ...adjustments.handlers,
  );

  const { queryClient } = renderApp([options.path ?? '/adjustments'], options.extra);

  return { user, accounting, adjustments, queryClient };
}

/** `text` with every run of whitespace as one space: ICU puts U+202F before "AM" and "PM". */
export function spaced(text: string | null): string {
  return (text ?? '').replace(/\s+/gu, ' ').trim();
}

/** Matches exactly `text`, whichever whitespace character stands where it has a space. */
export function exactly(text: string): RegExp {
  const escaped = text.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
  return new RegExp(`^${escaped.replace(/ /gu, '\\s')}$`, 'u');
}

/*
 * The page.
 */

/** The page's own section, named by its heading. */
export async function adjustmentsPage(): Promise<HTMLElement> {
  return screen.findByRole('region', { name: 'Adjustments' });
}

/** True when `earlier` comes before `later` in the document. */
export function precedes(earlier: Element, later: Element): boolean {
  return (earlier.compareDocumentPosition(later) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0;
}

/*
 * The form.
 */

export const FIELD_LABELS = [
  'Asset',
  'Quantity',
  'Unit cost (USD)',
  'Acquired on',
  'Note',
] as const;
export type FieldLabel = (typeof FIELD_LABELS)[number];

/** The one form of the page, whichever mode it is in. */
export function theForm(): HTMLElement {
  return screen.getByRole('form');
}

/** The form, once the page has rendered it. */
export async function formReady(name = 'Record an adjustment'): Promise<HTMLElement> {
  return screen.findByRole('form', { name });
}

export function field(label: FieldLabel): HTMLInputElement | HTMLTextAreaElement {
  return within(theForm()).getByLabelText<HTMLInputElement | HTMLTextAreaElement>(label);
}

/** What each of the five fields holds, by its label. */
export function fieldValues(): Record<FieldLabel, string> {
  return {
    Asset: field('Asset').value,
    Quantity: field('Quantity').value,
    'Unit cost (USD)': field('Unit cost (USD)').value,
    'Acquired on': field('Acquired on').value,
    Note: field('Note').value,
  };
}

export const EMPTY_FIELDS: Record<FieldLabel, string> = {
  Asset: '',
  Quantity: '',
  'Unit cost (USD)': '',
  'Acquired on': '',
  Note: '',
};

/** Sets the date field as a date picker does: one change, to a `datetime-local` value. */
export function setDate(value: string): void {
  fireEvent.change(field('Acquired on'), { target: { value } });
}

/** Replaces what a text field holds, by typing. */
export async function retype(
  user: UserEvent,
  label: Exclude<FieldLabel, 'Acquired on'>,
  value: string,
): Promise<void> {
  const input = field(label);
  await user.clear(input);
  if (value !== '') {
    await user.type(input, value);
  }
}

export interface Entry {
  readonly asset?: string;
  readonly quantity?: string;
  readonly unitCost?: string;
  /** A `datetime-local` value, in the zone the test runs under. */
  readonly occurredAt?: string;
  readonly note?: string;
}

/** A complete entry the server accepts, in any zone: acquired in February 2025. */
export const VALID_ENTRY = {
  asset: 'SOL',
  quantity: '2.5',
  unitCost: '140',
  occurredAt: '2025-02-27T09:30',
  note: 'Bought in person.',
} as const;

/** Fills the fields `entry` names, and leaves the others as they are. */
export async function fillForm(user: UserEvent, entry: Entry): Promise<void> {
  if (entry.asset !== undefined) {
    await retype(user, 'Asset', entry.asset);
  }
  if (entry.quantity !== undefined) {
    await retype(user, 'Quantity', entry.quantity);
  }
  if (entry.unitCost !== undefined) {
    await retype(user, 'Unit cost (USD)', entry.unitCost);
  }
  if (entry.occurredAt !== undefined) {
    setDate(entry.occurredAt);
  }
  if (entry.note !== undefined) {
    await retype(user, 'Note', entry.note);
  }
}

/** The form's submit button, whichever mode it is in. */
export function submitButton(): HTMLElement {
  return within(theForm()).getByRole('button', { name: /^(Record adjustment|Save changes)$/ });
}

/** The elements `input` is described by, in the order `aria-describedby` lists them. */
function describers(input: HTMLElement): HTMLElement[] {
  return (input.getAttribute('aria-describedby') ?? '')
    .split(/\s+/u)
    .filter((id) => id !== '')
    .map((id) => {
      const element = document.getElementById(id);
      if (element === null) {
        throw new Error(`aria-describedby names "${id}", and no element has that id.`);
      }
      return element;
    });
}

/**
 * The error shown under `label`, or `null`: the `role="alert"` the input is described by.
 * Fails when the relation is half made - an alert beside a field that is not marked invalid,
 * or an invalid field with no alert - because either half alone tells the owner nothing.
 */
export function fieldError(label: FieldLabel): string | null {
  const input = field(label);
  const wrapper = input.parentElement;
  if (wrapper === null) {
    throw new Error(`The ${label} field has no parent.`);
  }
  const described = describers(input).filter((element) => element.getAttribute('role') === 'alert');
  const beside = within(wrapper).queryAllByRole('alert');
  const invalid = input.getAttribute('aria-invalid');

  if (described.length > 1) {
    throw new Error(`The ${label} field is described by ${String(described.length)} alerts.`);
  }
  const [alert] = described;
  if (alert === undefined) {
    if (beside.length > 0) {
      throw new Error(`An alert sits beside ${label} that the field is not described by.`);
    }
    if (invalid !== null) {
      throw new Error(`${label} is aria-invalid="${invalid}" with no alert describing it.`);
    }
    return null;
  }
  if (invalid !== 'true') {
    throw new Error(`${label} is described by an alert and is not aria-invalid="true".`);
  }
  if (beside.length !== 1 || beside[0] !== alert) {
    throw new Error(`The alert describing ${label} is not the one beside it.`);
  }
  if (!precedes(input, alert)) {
    throw new Error(`The alert describing ${label} is above the field, not under it.`);
  }
  return alert.textContent;
}

/** The error under each of the five fields. */
export function fieldErrors(): Record<FieldLabel, string | null> {
  return {
    Asset: fieldError('Asset'),
    Quantity: fieldError('Quantity'),
    'Unit cost (USD)': fieldError('Unit cost (USD)'),
    'Acquired on': fieldError('Acquired on'),
    Note: fieldError('Note'),
  };
}

export const NO_FIELD_ERRORS: Record<FieldLabel, string | null> = {
  Asset: null,
  Quantity: null,
  'Unit cost (USD)': null,
  'Acquired on': null,
  Note: null,
};

/**
 * The messages at the bottom of the form: every alert inside it that belongs to no field.
 * Fails when one of them is not below the last field, which is where the spec puts them.
 */
export function formErrors(): string[] {
  const underFields = new Set(
    FIELD_LABELS.flatMap((label) =>
      describers(field(label)).filter((element) => element.getAttribute('role') === 'alert'),
    ),
  );
  const loose = within(theForm())
    .queryAllByRole('alert')
    .filter((alert) => !underFields.has(alert));

  for (const alert of loose) {
    if (!precedes(field('Note'), alert)) {
      throw new Error(`"${alert.textContent}" is not at the bottom of the form.`);
    }
  }
  return loose.map((alert) => alert.textContent);
}

/** The page's status line itself: every `role="status"` that is not the list's skeleton. */
export function statusElements(): HTMLElement[] {
  return screen
    .queryAllByRole('status')
    .filter((element) => !element.classList.contains('state-loading'));
}

/**
 * What the page's status line says, or `null` when it says nothing - whether because it is
 * empty or because it is not there. The list's skeleton is a `role="status"` too, and is not
 * this. Fails when two lines speak at once.
 */
export function statusLine(): string | null {
  const spoken = statusElements()
    .map((element) => element.textContent)
    .filter((text) => text !== '');
  if (spoken.length > 1) {
    throw new Error(`There are ${String(spoken.length)} status lines: ${spoken.join(' | ')}`);
  }
  return spoken[0] ?? null;
}

/*
 * The list.
 */

export const COLUMNS = ['Asset', 'Quantity', 'Unit cost (USD)', 'Acquired', 'Note', 'Actions'];
export type Column = 'Asset' | 'Quantity' | 'Unit cost (USD)' | 'Acquired' | 'Note' | 'Actions';

/** The scroll region that holds the table, named by the list's heading. */
export function listRegion(): HTMLElement {
  return screen.getByRole('region', { name: 'Recorded adjustments' });
}

/** The table, once the list has loaded with at least one adjustment. */
export async function loadedTable(): Promise<HTMLElement> {
  const region = await screen.findByRole('region', { name: 'Recorded adjustments' });
  return within(region).getByRole('table');
}

export function queryTable(): HTMLElement | null {
  return screen.queryByRole('table');
}

/** The body rows, in the order they are shown. */
export function rows(): HTMLElement[] {
  return within(listRegion()).getAllByRole('row').slice(1);
}

/** The asset of each row, in the order they are shown. */
export function shownAssets(): string[] {
  const table = queryTable();
  return table === null
    ? []
    : within(table)
        .getAllByRole('rowheader')
        .map((header) => header.textContent);
}

/** The one row of `asset`. Fails when there is none, or more than one. */
export function rowOf(asset: string): HTMLElement {
  const row = within(listRegion()).getByRole('rowheader', { name: asset }).closest('tr');
  if (row === null) {
    throw new Error(`The row header "${asset}" is not inside a row.`);
  }
  return row;
}

/** The cell of `row` under the column headed `column`. */
export function cell(row: HTMLElement, column: Column): HTMLElement {
  const headers = within(listRegion())
    .getAllByRole('columnheader')
    .map((header) => header.textContent);
  const found = Array.from(row.children)[headers.indexOf(column)];
  if (!(found instanceof HTMLElement)) {
    throw new Error(`The row has no cell under "${column}". Columns: ${headers.join(', ')}.`);
  }
  return found;
}

/** The exact string every `<data value>` inside `element` carries. */
export function dataValues(element: HTMLElement): (string | null)[] {
  return Array.from(element.querySelectorAll('data')).map((data) => data.getAttribute('value'));
}

export type RowAction = 'Edit' | 'Delete' | 'Confirm delete' | 'Cancel';

/** A row's control, by the text it shows. Its accessible name says which row it acts on. */
export function rowButton(row: HTMLElement, action: RowAction): HTMLElement {
  const button = queryRowButton(row, action);
  if (button === null) {
    throw new Error(`The row has no "${action}" button. It reads: ${spaced(row.textContent)}`);
  }
  return button;
}

export function queryRowButton(row: HTMLElement, action: RowAction): HTMLElement | null {
  return (
    within(row)
      .queryAllByRole('button')
      .find((button) => button.textContent === action) ?? null
  );
}

/** The text of each button a row shows, in order. */
export function rowButtons(row: HTMLElement): string[] {
  return within(row)
    .queryAllByRole('button')
    .map((button) => button.textContent);
}

/** Starts editing the row of `asset`. */
export async function startEditing(user: UserEvent, asset: string): Promise<HTMLElement> {
  await loadedTable();
  await user.click(rowButton(rowOf(asset), 'Edit'));
  return screen.getByRole('form', { name: 'Edit adjustment' });
}

/*
 * The failed-recompute alert.
 */

/** The alert about the last recompute, or `null`. */
export function recomputeAlert(): HTMLElement | null {
  return (
    screen
      .queryAllByRole('alert')
      .find((alert) => alert.textContent.startsWith('The last recompute of the positions')) ?? null
  );
}
