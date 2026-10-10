import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, type HttpHandler } from 'msw';
import { describe, expect, it } from 'vitest';

import {
  IMPORTS_PATH,
  OPERATIONS_PATH,
  type ImportFileReport,
  type ImportReport,
  type Operation,
} from '@/api/operations';
import { describeFile, OPERATIONS_LOADING_LABEL } from '@/pages/OperationsPage';
import {
  fakeOperations,
  type FakeOperations,
  type FakeOperationsOptions,
} from '@/test/fakeOperations';
import { fakePortfolio } from '@/test/fakePortfolio';
import { healthyPortfolio } from '@/test/fixtures';
import { operation } from '@/test/investmentFixtures';
import { renderApp, type ProvidedRender } from '@/test/render';
import { fakeSession, problem, server, TEST_USERNAME } from '@/test/server';
import { inTimeZone } from '@/test/timeZone';

/**
 * The Operations page (spec 042): uploading the exchanges' reports, adding what no report
 * covers by hand, and the stored operations in a table with its four states.
 */

interface Opened extends ProvidedRender {
  readonly user: ReturnType<typeof userEvent.setup>;
  readonly fake: FakeOperations;
}

function openOperations(
  options: FakeOperationsOptions = {},
  overrides: readonly HttpHandler[] = [],
): Opened {
  const user = userEvent.setup();
  const fake = fakeOperations(options);
  server.use(
    ...overrides,
    ...fakeSession({ initialUser: TEST_USERNAME }).handlers,
    ...fake.handlers,
    ...fakePortfolio(healthyPortfolio()).handlers,
  );
  return Object.assign(renderApp(['/operations']), { user, fake });
}

async function settledPage(): Promise<void> {
  await screen.findByRole('heading', { name: 'Operations', level: 2 });
  await waitFor(() => {
    expect(screen.queryByText(OPERATIONS_LOADING_LABEL)).not.toBeInTheDocument();
  });
}

function table(): HTMLElement {
  return screen.getByRole('table', { name: 'Stored operations, newest first' });
}

/** Every cell of the `index`th row whose asset is `asset`: when, where, kind, then the rest. */
function cellsOf(asset: string, index = 0): string[] {
  const header = within(table()).getAllByRole('rowheader', { name: asset })[index];
  return within(header?.closest('tr') as HTMLElement)
    .getAllByRole('cell')
    .map((cell) => cell.textContent);
}

/** The cells after the asset: quantity, paid or received, fee, note and actions. */
function rowOf(asset: string, index = 0): string[] {
  return cellsOf(asset, index).slice(3);
}

const BITGET_FILE: ImportFileReport = {
  name: 'bitget.csv',
  format: 'bitget_spot_order_details',
  rows: 2,
  stored: 2,
  already_stored: 0,
  skipped_reason: null,
};

function report(overrides: Partial<ImportReport> = {}): ImportReport {
  return {
    filename: 'bitget.csv',
    stored: 2,
    already_stored: 0,
    files: [BITGET_FILE],
    ...overrides,
  };
}

function csv(name: string, text = 'a,b\n1,2\n'): File {
  return new File([text], name, { type: 'text/csv' });
}

const DEPOSIT: Operation = operation({
  id: 2,
  executed_at: '2026-05-02T09:00:00Z',
  kind: 'deposit',
  asset: 'KAS',
  quantity: '1500.000000000000000000',
  quote_currency: null,
  quote_amount: null,
  fee_asset: null,
  fee_amount: null,
  description: 'Deposit',
});

const MANUAL: Operation = operation({
  id: 3,
  source: 'manual',
  venue: 'Tangem',
  external_id: 'manual:abc',
  executed_at: '2026-05-03T09:00:00Z',
  asset: 'KAS',
  quantity: '6100.000000000000000000',
  quote_amount: '500.000000000000000000',
  fee_asset: null,
  fee_amount: null,
  description: 'Swap in Tangem',
  manual: true,
});

describe('describeFile', () => {
  it('says what each file held, or why it was skipped', () => {
    expect(describeFile(BITGET_FILE)).toBe('bitget.csv: 2 new, 0 already stored.');
    expect(
      describeFile({
        name: 'ledger.csv',
        format: null,
        rows: 1,
        stored: 0,
        already_stored: 0,
        skipped_reason: 'not a format this importer reads',
      }),
    ).toBe('ledger.csv: skipped, not a format this importer reads (1 row).');
    expect(
      describeFile({
        name: 'notes.txt',
        format: null,
        rows: 3,
        stored: 0,
        already_stored: 0,
        skipped_reason: null,
      }),
    ).toBe('notes.txt: skipped, not read (3 rows).');
  });
});

describe('OperationsPage: the table', () => {
  it('announces the wait while the operations load', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    openOperations({ operations: [operation()] }, [
      http.get(OPERATIONS_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);

    expect(await screen.findByText(OPERATIONS_LOADING_LABEL)).toHaveAttribute('role', 'status');

    release();
    await settledPage();
    expect(table()).toBeInTheDocument();
  });

  it('says so while nothing is stored, rather than an empty table', async () => {
    openOperations();

    await settledPage();

    expect(screen.getByRole('heading', { name: 'No operations yet' })).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });

  it('lists every operation newest first, with a dash where there is no counterpart', async () => {
    inTimeZone('UTC');
    openOperations({ operations: [operation(), DEPOSIT, MANUAL] });

    await settledPage();

    const section = screen.getByRole('region', { name: 'Operations' });
    expect(within(section).getByText('3 operations')).toBeInTheDocument();
    expect(
      within(table())
        .getAllByRole('rowheader')
        .map((header) => header.textContent),
    ).toEqual(['KAS', 'KAS', 'BTC']);
    expect(rowOf('BTC')).toEqual(['0.5 BTC', '20,000 USDT', '0.0005 BTC', 'Spot Buy', '']);
    expect(rowOf('KAS', 1)).toEqual(['1,500 KAS', '—', '—', 'Deposit', '']);
    expect(cellsOf('BTC').slice(0, 3)).toEqual(['May 1, 2026, 12:00 PM', 'Bitget', 'Buy']);
    // Only an entry made by hand can be deleted: an upload would bring the others back.
    expect(within(table()).getAllByRole('button', { name: 'Delete' })).toHaveLength(1);
  });

  it('counts a single operation in the singular', async () => {
    openOperations({ operations: [operation()] });

    await settledPage();

    expect(screen.getByText('1 operation')).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: 'Pages' })).not.toBeInTheDocument();
  });

  it('pages through more operations than fit on one page', async () => {
    const many = Array.from({ length: 51 }, (_, index) =>
      operation({
        id: index + 1,
        executed_at: `2026-05-01T00:${String(index).padStart(2, '0')}:00Z`,
        quantity: `${String(index + 1)}.000000000000000000`,
      }),
    );
    const { user } = openOperations({ operations: many });
    await settledPage();
    const pages = screen.getByRole('navigation', { name: 'Pages' });

    expect(pages).toHaveTextContent('Page 1 of 2');
    expect(within(pages).getByRole('button', { name: 'Newer' })).toBeDisabled();
    expect(within(table()).getAllByRole('rowheader')).toHaveLength(50);

    await user.click(within(pages).getByRole('button', { name: 'Older' }));

    await waitFor(() => {
      expect(pages).toHaveTextContent('Page 2 of 2');
    });
    await waitFor(() => {
      expect(rowOf('BTC')[0]).toBe('1 BTC');
    });
    expect(within(pages).getByRole('button', { name: 'Older' })).toBeDisabled();

    await user.click(within(pages).getByRole('button', { name: 'Newer' }));

    await waitFor(() => {
      expect(rowOf('BTC')[0]).toBe('51 BTC');
    });
  });

  it('says what failed and loads again on request', async () => {
    let fail = true;
    const { user } = openOperations({ operations: [operation()] }, [
      http.get(OPERATIONS_PATH, () =>
        fail ? problem(500, 'Internal Server Error', 'The database is locked.') : undefined,
      ),
    ]);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load the operations');
    expect(alert).toHaveTextContent('The database is locked.');

    fail = false;
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));

    expect(await screen.findByRole('table')).toBeInTheDocument();
  });

  it('deletes an entry made by hand', async () => {
    const { user, fake } = openOperations({ operations: [operation(), MANUAL] });
    await settledPage();

    await user.click(within(table()).getByRole('button', { name: 'Delete' }));

    await waitFor(() => {
      expect(screen.getByText('1 operation')).toBeInTheDocument();
    });
    expect(within(table()).queryByRole('rowheader', { name: 'KAS' })).not.toBeInTheDocument();
    expect(
      fake.requests.filter((request) => request.method === 'DELETE').map((request) => request.url),
    ).toEqual([`http://localhost:3000${OPERATIONS_PATH}/3`]);
  });

  it('says why an entry was not deleted, and keeps it', async () => {
    const { user } = openOperations({ operations: [MANUAL] }, [
      http.delete(`${OPERATIONS_PATH}/:id`, () =>
        problem(409, 'Conflict', 'Only a manual entry can be deleted.'),
      ),
    ]);
    await settledPage();

    await user.click(within(table()).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Not deleted: Only a manual entry can be deleted.',
    );
    expect(within(table()).getByRole('rowheader', { name: 'KAS' })).toBeInTheDocument();
  });
});

describe('OperationsPage: uploading reports', () => {
  function form(): HTMLElement {
    return screen.getByRole('form', { name: 'Upload exchange reports' });
  }

  it('uploads each file in turn and says what each held', async () => {
    const { user, fake } = openOperations({
      uploads: [
        report(),
        report({
          filename: 'exports.zip',
          stored: 0,
          already_stored: 3,
          files: [
            {
              name: 'binance.csv',
              format: 'binance_transaction_history',
              rows: 3,
              stored: 0,
              already_stored: 3,
              skipped_reason: null,
            },
            {
              name: 'ledger.csv',
              format: null,
              rows: 1,
              stored: 0,
              already_stored: 0,
              skipped_reason: 'not a format this importer reads',
            },
          ],
        }),
      ],
    });
    await settledPage();
    const upload = within(form()).getByRole('button', { name: 'Upload' });
    expect(upload).toBeDisabled();

    await user.upload(within(form()).getByLabelText('Files'), [
      csv('bitget.csv', 'héllo'),
      new File(['PK'], 'exports.zip', { type: 'application/zip' }),
    ]);
    await user.click(upload);

    const uploaded = await within(form()).findByRole('list', { name: 'Uploaded' });
    await waitFor(() => {
      expect(
        within(uploaded)
          .getAllByRole('listitem')
          .map((item) => item.textContent),
      ).toEqual([
        'bitget.csv: 2 new, 0 already stored.',
        'binance.csv: 0 new, 3 already stored.',
        'ledger.csv: skipped, not a format this importer reads (1 row).',
      ]);
    });
    const sent = fake.requests.filter((request) => request.method === 'POST');
    expect(sent.map((request) => request.body)).toEqual([
      // The bytes as they are, UTF-8 included: base64 of "héllo".
      { filename: 'bitget.csv', content_base64: 'aMOpbGxv' },
      { filename: 'exports.zip', content_base64: 'UEs=' },
    ]);
    expect(within(form()).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('stops at a file the server refuses, and says why', async () => {
    const { user, fake } = openOperations({
      uploads: [
        report(),
        problem(422, 'Unprocessable Entity', 'binance.csv, line 1: the file name has no zone.'),
        report(),
      ],
    });
    await settledPage();

    await user.upload(within(form()).getByLabelText('Files'), [
      csv('bitget.csv'),
      csv('binance.csv'),
      csv('nexo.csv'),
    ]);
    await user.click(within(form()).getByRole('button', { name: 'Upload' }));

    expect(await within(form()).findByRole('alert')).toHaveTextContent(
      'Nothing stored from this file: binance.csv, line 1: the file name has no zone.',
    );
    expect(
      within(within(form()).getByRole('list', { name: 'Uploaded' }))
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['bitget.csv: 2 new, 0 already stored.']);
    expect(fake.requests.filter((request) => request.method === 'POST')).toHaveLength(2);
  });

  it('announces an upload while it runs', async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { user } = openOperations({ uploads: [report()] }, [
      http.post(IMPORTS_PATH, async () => {
        await gate;
        return undefined;
      }),
    ]);
    await settledPage();

    await user.upload(within(form()).getByLabelText('Files'), csv('bitget.csv'));
    await user.click(within(form()).getByRole('button', { name: 'Upload' }));

    expect(await within(form()).findByRole('status')).toHaveTextContent('Uploading…');
    expect(within(form()).getByRole('button', { name: 'Upload' })).toBeDisabled();

    release();
    expect(await within(form()).findByRole('list', { name: 'Uploaded' })).toBeInTheDocument();
  });

  it('offers nothing to upload once the choice is cleared', async () => {
    const { user } = openOperations();
    await settledPage();
    const input = within(form()).getByLabelText('Files');
    await user.upload(input, csv('bitget.csv'));
    expect(within(form()).getByRole('button', { name: 'Upload' })).toBeEnabled();

    fireEvent.change(input, { target: { files: null } });

    expect(within(form()).getByRole('button', { name: 'Upload' })).toBeDisabled();
  });
});

describe('OperationsPage: an operation by hand', () => {
  function form(): HTMLElement {
    return screen.getByRole('form', { name: 'Add an operation by hand' });
  }

  async function fill(user: ReturnType<typeof userEvent.setup>): Promise<void> {
    const fields = within(form());
    fireEvent.change(fields.getByLabelText('When'), { target: { value: '2026-05-10T14:30' } });
    await user.selectOptions(fields.getByLabelText('Kind'), 'sell');
    await user.selectOptions(fields.getByLabelText('Kind'), 'buy');
    await user.type(fields.getByLabelText('Asset'), ' kas ');
    await user.type(fields.getByLabelText('Quantity'), '6100');
    await user.type(fields.getByLabelText('Amount paid or received'), '500');
    await user.type(fields.getByLabelText('Note'), 'Swap in Tangem');
  }

  it('adds a swap no report covers, in the zone of this browser', async () => {
    inTimeZone('America/Argentina/Buenos_Aires');
    const { user, fake } = openOperations();
    await settledPage();
    const add = within(form()).getByRole('button', { name: 'Add' });
    expect(add).toBeDisabled();

    await fill(user);
    await user.click(add);

    expect(await within(form()).findByRole('status')).toHaveTextContent('Added.');
    expect(fake.requests.find((request) => request.method === 'POST')?.body).toEqual({
      venue: 'Tangem',
      executed_at: '2026-05-10T17:30:00.000Z',
      kind: 'buy',
      asset: 'kas',
      quantity: '6100',
      quote_currency: 'USDT',
      quote_amount: '500',
      description: 'Swap in Tangem',
    });
    expect(within(form()).getByLabelText('Asset')).toHaveValue('');
    expect(within(form()).getByLabelText('Where')).toHaveValue('Tangem');
    expect(await screen.findByRole('table')).toBeInTheDocument();
    expect(rowOf('KAS')[4]).toBe('Delete');
  });

  it('sends a sell as a sell, from any venue', async () => {
    const { user, fake } = openOperations();
    await settledPage();
    const fields = within(form());

    await user.clear(fields.getByLabelText('Where'));
    await user.type(fields.getByLabelText('Where'), 'Exodus');
    await user.clear(fields.getByLabelText('Paid or received in'));
    await user.type(fields.getByLabelText('Paid or received in'), 'USDC');
    await fill(user);
    await user.selectOptions(fields.getByLabelText('Kind'), 'sell');
    await user.click(fields.getByRole('button', { name: 'Add' }));

    await within(form()).findByRole('status');
    expect(fake.requests.find((request) => request.method === 'POST')?.body).toMatchObject({
      venue: 'Exodus',
      kind: 'sell',
      quote_currency: 'USDC',
    });
  });

  it('says why an operation was not added, and keeps what was typed', async () => {
    const { user } = openOperations({}, [
      http.post(OPERATIONS_PATH, () =>
        problem(422, 'Unprocessable Entity', 'quantity: must be greater than 0.'),
      ),
    ]);
    await settledPage();

    await fill(user);
    await user.click(within(form()).getByRole('button', { name: 'Add' }));

    expect(await within(form()).findByRole('alert')).toHaveTextContent(
      'Not added: quantity: must be greater than 0.',
    );
    expect(within(form()).getByLabelText('Quantity')).toHaveValue('6100');
  });

  it('says so when the server could not be reached', async () => {
    const { user } = openOperations({}, [http.post(OPERATIONS_PATH, () => HttpResponse.error())]);
    await settledPage();

    await fill(user);
    await user.click(within(form()).getByRole('button', { name: 'Add' }));

    expect(await within(form()).findByRole('alert')).toHaveTextContent(
      'Not added: The server could not be reached.',
    );
  });
});
