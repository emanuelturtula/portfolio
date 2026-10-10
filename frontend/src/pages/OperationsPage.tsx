import { useState, type SubmitEvent } from 'react';

import { describeApiError } from '@/api/client';
import {
  OPERATIONS_PAGE_SIZE,
  useCreateManualOperation,
  useDeleteOperation,
  useImportOperations,
  useOperations,
  type ImportFileReport,
  type ImportReport,
  type Operation,
} from '@/api/operations';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { formatQuantity } from '@/lib/investment';
import { money } from '@/lib/money';

export const OPERATIONS_LOADING_LABEL = 'Loading the operations…';

const LOAD_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const WRITE_FAILURE_FALLBACK = 'The server could not be reached.';

/** The kinds as the table words them. */
const KIND_LABELS: Record<Operation['kind'], string> = {
  buy: 'Buy',
  sell: 'Sell',
  reward: 'Reward',
  deposit: 'Deposit',
  withdrawal: 'Withdrawal',
  transfer: 'Transfer',
  other: 'Other',
};

/** One file of an upload in a sentence: what was new, or why it was skipped. */
export function describeFile(file: ImportFileReport): string {
  if (file.format === null) {
    const rows = file.rows === 1 ? '1 row' : `${String(file.rows)} rows`;
    return `${file.name}: skipped, ${file.skipped_reason ?? 'not read'} (${rows}).`;
  }
  return `${file.name}: ${String(file.stored)} new, ${String(file.already_stored)} already stored.`;
}

/**
 * Uploads every chosen file, one after the other, and lists what each held. A file the backend
 * refuses stops the run there and says why: the files before it are stored, it and the ones
 * after it are not.
 */
function UploadForm() {
  const upload = useImportOperations();
  const [files, setFiles] = useState<readonly File[]>([]);
  const [reports, setReports] = useState<readonly ImportReport[]>([]);

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setReports([]);
    const done: ImportReport[] = [];
    for (const file of files) {
      try {
        done.push(await upload.mutateAsync(file));
      } catch {
        break;
      } finally {
        setReports([...done]);
      }
    }
  }

  return (
    <form
      className="card"
      onSubmit={(event) => {
        void handleSubmit(event);
      }}
      noValidate
      aria-labelledby="upload-heading"
    >
      <h3 id="upload-heading">Upload exchange reports</h3>
      <p className="hint">
        A CSV or a zip, as Bitget, BingX, Binance or Nexo served it. Operations already stored are
        skipped, so uploading a report again is safe.
      </p>
      <div className="field">
        <label htmlFor="operations-files">Files</label>
        <input
          id="operations-files"
          type="file"
          accept=".csv,.zip,text/csv,application/zip"
          multiple
          onChange={(event) => {
            setFiles(Array.from(event.target.files ?? []));
            upload.reset();
          }}
        />
      </div>
      <button
        type="submit"
        className="button-primary"
        disabled={files.length === 0 || upload.isPending}
      >
        Upload
      </button>
      {upload.isPending && (
        <p className="note" role="status">
          Uploading…
        </p>
      )}
      {upload.isError && (
        <p className="note note-error" role="alert">
          Nothing stored from this file: {describeApiError(upload.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
      {reports.length > 0 && (
        <ul className="upload-report" aria-label="Uploaded">
          {reports.flatMap((report) =>
            report.files.map((file) => (
              <li key={`${report.filename}/${file.name}`}>{describeFile(file)}</li>
            )),
          )}
        </ul>
      )}
    </form>
  );
}

/** `datetime-local` gives a wall-clock time with no zone: it is this browser's. */
function toInstant(local: string): string {
  return new Date(local).toISOString();
}

/** A buy or a sell no export covers, such as a swap inside a wallet app (R11). */
function ManualForm() {
  const create = useCreateManualOperation();
  const [venue, setVenue] = useState('Tangem');
  const [executedAt, setExecutedAt] = useState('');
  const [kind, setKind] = useState<'buy' | 'sell'>('buy');
  const [asset, setAsset] = useState('');
  const [quantity, setQuantity] = useState('');
  const [quoteCurrency, setQuoteCurrency] = useState('USDT');
  const [quoteAmount, setQuoteAmount] = useState('');
  const [description, setDescription] = useState('');
  const complete = [venue, executedAt, asset, quantity, quoteCurrency, quoteAmount].every(
    (value) => value.trim() !== '',
  );

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(
      {
        venue: venue.trim(),
        executed_at: toInstant(executedAt),
        kind,
        asset: asset.trim(),
        quantity: quantity.trim(),
        quote_currency: quoteCurrency.trim(),
        quote_amount: quoteAmount.trim(),
        description: description.trim(),
      },
      {
        onSuccess: () => {
          setAsset('');
          setQuantity('');
          setQuoteAmount('');
          setDescription('');
        },
      },
    );
  }

  return (
    <form className="card" onSubmit={handleSubmit} noValidate aria-labelledby="manual-heading">
      <h3 id="manual-heading">Add an operation by hand</h3>
      <p className="hint">
        For a buy or a sell no report covers, such as a swap inside a wallet app. Amounts are before
        any fee.
      </p>
      <div className="field">
        <label htmlFor="manual-venue">Where</label>
        <input
          id="manual-venue"
          type="text"
          value={venue}
          onChange={(event) => {
            setVenue(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-when">When</label>
        <input
          id="manual-when"
          type="datetime-local"
          value={executedAt}
          onChange={(event) => {
            setExecutedAt(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-kind">Kind</label>
        <select
          id="manual-kind"
          value={kind}
          onChange={(event) => {
            setKind(event.target.value === 'sell' ? 'sell' : 'buy');
          }}
        >
          <option value="buy">Buy</option>
          <option value="sell">Sell</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="manual-asset">Asset</label>
        <input
          id="manual-asset"
          type="text"
          value={asset}
          autoCapitalize="characters"
          onChange={(event) => {
            setAsset(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-quantity">Quantity</label>
        <input
          id="manual-quantity"
          type="text"
          inputMode="decimal"
          value={quantity}
          onChange={(event) => {
            setQuantity(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-quote">Paid or received in</label>
        <input
          id="manual-quote"
          type="text"
          value={quoteCurrency}
          onChange={(event) => {
            setQuoteCurrency(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-amount">Amount paid or received</label>
        <input
          id="manual-amount"
          type="text"
          inputMode="decimal"
          value={quoteAmount}
          onChange={(event) => {
            setQuoteAmount(event.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="manual-description">Note</label>
        <input
          id="manual-description"
          type="text"
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
        />
      </div>
      <button type="submit" className="button-primary" disabled={!complete || create.isPending}>
        Add
      </button>
      {create.isError && (
        <p className="note note-error" role="alert">
          Not added: {describeApiError(create.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
      {create.isSuccess && (
        <p className="note" role="status">
          Added.
        </p>
      )}
    </form>
  );
}

/** What was paid or received for a trade, or a dash for an operation that has no counterpart. */
function quoteOf(operation: Operation): string {
  if (operation.quote_amount === null || operation.quote_currency === null) {
    return '—';
  }
  return formatQuantity(money(operation.quote_amount), operation.quote_currency);
}

function feeOf(operation: Operation): string {
  if (operation.fee_amount === null || operation.fee_asset === null) {
    return '—';
  }
  return formatQuantity(money(operation.fee_amount), operation.fee_asset);
}

interface OperationRowProps {
  readonly operation: Operation;
  readonly onDelete: (id: number) => void;
  readonly deleting: boolean;
}

function OperationRow({ operation, onDelete, deleting }: OperationRowProps) {
  return (
    <tr>
      <td>
        <AbsoluteTime value={operation.executed_at} />
      </td>
      <td>{operation.venue}</td>
      <td>{KIND_LABELS[operation.kind]}</td>
      <th scope="row">{operation.asset}</th>
      <td className="num">{formatQuantity(money(operation.quantity), operation.asset)}</td>
      <td className="num">{quoteOf(operation)}</td>
      <td className="num">{feeOf(operation)}</td>
      <td>{operation.description}</td>
      <td>
        {operation.manual && (
          <button
            type="button"
            disabled={deleting}
            onClick={() => {
              onDelete(operation.id);
            }}
          >
            Delete
          </button>
        )}
      </td>
    </tr>
  );
}

/** The stored operations, every asset, newest first, a page at a time; four states. */
function OperationsTable() {
  const [page, setPage] = useState(0);
  const operations = useOperations(page);
  const remove = useDeleteOperation();

  if (operations.isPending) {
    return <Skeleton label={OPERATIONS_LOADING_LABEL} />;
  }

  if (operations.isError) {
    return (
      <ErrorState
        title="Could not load the operations"
        headingLevel={3}
        description={describeApiError(operations.error, LOAD_FAILURE_FALLBACK)}
        onRetry={() => {
          void operations.refetch();
        }}
      />
    );
  }

  const { count, operations: rows } = operations.data;
  if (count === 0) {
    return (
      <EmptyState
        title="No operations yet"
        description="Upload an exchange report above, or add an operation by hand."
        headingLevel={3}
      />
    );
  }

  const pages = Math.ceil(count / OPERATIONS_PAGE_SIZE);
  return (
    <section
      className="card"
      aria-labelledby="operations-heading"
      aria-busy={operations.isPlaceholderData}
    >
      <div className="card-head">
        <h3 id="operations-heading">Operations</h3>
        <span className="page-meta">
          {count === 1 ? '1 operation' : `${String(count)} operations`}
        </span>
      </div>
      {remove.isError && (
        <p className="note note-error" role="alert">
          Not deleted: {describeApiError(remove.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
      <div className="table-scroll">
        <table className="data-table">
          <caption className="visually-hidden">Stored operations, newest first</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Where</th>
              <th scope="col">Kind</th>
              <th scope="col">Asset</th>
              <th scope="col" className="num">
                Quantity
              </th>
              <th scope="col" className="num">
                Paid or received
              </th>
              <th scope="col" className="num">
                Fee
              </th>
              <th scope="col">Note</th>
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((operation) => (
              <OperationRow
                key={operation.id}
                operation={operation}
                deleting={remove.isPending}
                onDelete={(id) => {
                  remove.mutate(id);
                }}
              />
            ))}
          </tbody>
        </table>
      </div>
      {pages > 1 && (
        <nav className="state-actions" aria-label="Pages">
          <button
            type="button"
            disabled={page === 0}
            onClick={() => {
              setPage(page - 1);
            }}
          >
            Newer
          </button>
          <span>
            Page {page + 1} of {pages}
          </span>
          <button
            type="button"
            disabled={page + 1 >= pages}
            onClick={() => {
              setPage(page + 1);
            }}
          >
            Older
          </button>
        </nav>
      )}
    </section>
  );
}

/**
 * The Operations page (spec 042): upload the exchanges' reports, add what no report covers, and
 * every stored operation in a table. The dashboard's Invested section is built from these.
 */
export function OperationsPage() {
  return (
    <div className="page">
      <div className="page-head">
        <h2 className="page-title">Operations</h2>
      </div>
      <div className="side-layout">
        <UploadForm />
        <ManualForm />
      </div>
      <OperationsTable />
    </div>
  );
}
