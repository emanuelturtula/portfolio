import { useState, type SubmitEvent } from 'react';

import { describeApiError } from '@/api/client';
import {
  DEFAULT_OPERATIONS_PAGE_SIZE,
  OPERATIONS_PAGE_SIZES,
  startOfDay,
  useCreateManualOperation,
  useDeleteOperation,
  useImportOperations,
  useOperations,
  type ImportFileReport,
  type ImportReport,
  type Operation,
  type OperationsPageSize,
} from '@/api/operations';
import { useMoveOperations, type Portfolio } from '@/api/portfolios';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import {
  PortfolioField,
  PortfolioSelect,
  portfolioBody,
  usePortfolioTarget,
} from '@/components/PortfolioField';
import { Skeleton } from '@/components/Skeleton';
import { formatQuantity } from '@/lib/investment';
import { money } from '@/lib/money';
import { usePortfolioScope } from '@/lib/portfolioScope';

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
  fee: 'Network fee',
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
  const target = usePortfolioTarget();
  const [files, setFiles] = useState<readonly File[]>([]);
  const [reports, setReports] = useState<readonly ImportReport[]>([]);

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    setReports([]);
    const done: ImportReport[] = [];
    for (const file of files) {
      try {
        done.push(await upload.mutateAsync({ file, ...portfolioBody(target) }));
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
      <PortfolioField id="operations-portfolio" target={target} />
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

/** The kinds an entry by hand can be, in the order the form offers them (spec 043). */
const MANUAL_KINDS = ['buy', 'sell', 'reward', 'fee'] as const;
type ManualKind = (typeof MANUAL_KINDS)[number];

function manualKindOf(value: string): ManualKind {
  return MANUAL_KINDS.find((kind) => kind === value) ?? 'buy';
}

/**
 * What no export covers (R11): a buy or a sell, such as a swap inside a wallet app, or, with no
 * counterpart, a miner's reward or a network fee a withdrawal paid that its report left out.
 */
function ManualForm() {
  const create = useCreateManualOperation();
  const target = usePortfolioTarget();
  const [venue, setVenue] = useState('Tangem');
  const [executedAt, setExecutedAt] = useState('');
  const [kind, setKind] = useState<ManualKind>('buy');
  const [asset, setAsset] = useState('');
  const [quantity, setQuantity] = useState('');
  const [quoteCurrency, setQuoteCurrency] = useState('USDT');
  const [quoteAmount, setQuoteAmount] = useState('');
  const [description, setDescription] = useState('');
  const trade = kind === 'buy' || kind === 'sell';
  const required = trade
    ? [venue, executedAt, asset, quantity, quoteCurrency, quoteAmount]
    : [venue, executedAt, asset, quantity];
  const complete = required.every((value) => value.trim() !== '');

  function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    create.mutate(
      {
        venue: venue.trim(),
        executed_at: toInstant(executedAt),
        kind,
        asset: asset.trim(),
        quantity: quantity.trim(),
        // A reward or a fee has no counterpart, and the server refuses one sent anyway.
        ...(trade
          ? { quote_currency: quoteCurrency.trim(), quote_amount: quoteAmount.trim() }
          : {}),
        description: description.trim(),
        ...portfolioBody(target),
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
        For what no report covers: a swap inside a wallet app, what a miner paid you, or a network
        fee a withdrawal paid that its report left out. Amounts are before any fee.
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
            setKind(manualKindOf(event.target.value));
          }}
        >
          {MANUAL_KINDS.map((value) => (
            <option key={value} value={value}>
              {KIND_LABELS[value]}
            </option>
          ))}
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
      {trade && (
        <>
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
        </>
      )}
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
      <PortfolioField id="manual-portfolio" target={target} />
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
  /** The portfolios, when the table shows which one each row is in; otherwise `undefined`. */
  readonly portfolios: readonly Portfolio[] | undefined;
}

function OperationRow({ operation, onDelete, deleting, portfolios }: OperationRowProps) {
  return (
    <tr>
      <td>
        <AbsoluteTime value={operation.executed_at} />
      </td>
      {portfolios !== undefined && (
        <td>{portfolios.find((portfolio) => portfolio.id === operation.portfolio_id)?.name}</td>
      )}
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

interface Filters {
  readonly asset: string;
  readonly venue: string;
  readonly from: string;
  readonly to: string;
}

const NO_FILTERS: Filters = { asset: '', venue: '', from: '', to: '' };

function filtering(filters: Filters): boolean {
  return Object.values(filters).some((value) => value !== '');
}

/** "Showing 51–100 of 912": the rows on screen and how many the filters keep. */
export function describeShown(page: number, pageSize: number, shown: number, count: number) {
  const first = page * pageSize + 1;
  const last = page * pageSize + shown;
  return first === last
    ? `Showing ${String(first)} of ${String(count)}`
    : `Showing ${String(first)}–${String(last)} of ${String(count)}`;
}

interface FilterBarProps {
  readonly filters: Filters;
  readonly assets: readonly string[];
  readonly venues: readonly string[];
  readonly onChange: (filters: Filters) => void;
}

/** The table's filters: a range of days, an asset and a venue. */
function FilterBar({ filters, assets, venues, onChange }: FilterBarProps) {
  return (
    <div className="operations-filters" role="group" aria-label="Filters">
      <div className="field">
        <label htmlFor="operations-from">From</label>
        <input
          id="operations-from"
          type="date"
          value={filters.from}
          max={filters.to === '' ? undefined : filters.to}
          onChange={(event) => {
            const from = event.target.value;
            // A start after the end would ask for an empty window: the end gives way.
            const to = filters.to !== '' && filters.to < from ? '' : filters.to;
            onChange({ ...filters, from, to });
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="operations-to">To</label>
        <input
          id="operations-to"
          type="date"
          value={filters.to}
          min={filters.from === '' ? undefined : filters.from}
          onChange={(event) => {
            const to = event.target.value;
            const from = filters.from !== '' && to !== '' && filters.from > to ? '' : filters.from;
            onChange({ ...filters, from, to });
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="operations-asset">Asset</label>
        <select
          id="operations-asset"
          value={filters.asset}
          onChange={(event) => {
            onChange({ ...filters, asset: event.target.value });
          }}
        >
          <option value="">All assets</option>
          {assets.map((asset) => (
            <option key={asset} value={asset}>
              {asset}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label htmlFor="operations-venue">Where</label>
        <select
          id="operations-venue"
          value={filters.venue}
          onChange={(event) => {
            onChange({ ...filters, venue: event.target.value });
          }}
        >
          <option value="">Everywhere</option>
          {venues.map((venue) => (
            <option key={venue} value={venue}>
              {venue}
            </option>
          ))}
        </select>
      </div>
      <button
        type="button"
        disabled={!filtering(filters)}
        onClick={() => {
          onChange(NO_FILTERS);
        }}
      >
        Clear filters
      </button>
    </div>
  );
}

interface MoveBarProps {
  readonly count: number;
  readonly scope: number | null;
  readonly filters: Filters;
  /** Every portfolio but the one shown, which is where they can go. */
  readonly choices: readonly Portfolio[];
  /** The first of `choices`, the default. */
  readonly first: Portfolio;
}

/**
 * Moves every operation the filters keep, on every page, to another portfolio (spec 044, R7).
 * It names how many before it runs, and moving them back is the same action.
 */
function MoveBar({ count, scope, filters, choices, first }: MoveBarProps) {
  const move = useMoveOperations();
  const [picked, setPicked] = useState<number | undefined>(undefined);
  const target = choices.find((portfolio) => portfolio.id === picked) ?? first;
  const noun = count === 1 ? 'operation' : 'operations';

  return (
    <div className="operations-move" role="group" aria-label="Move to another portfolio">
      <PortfolioSelect
        id="operations-move-target"
        label={`Move these ${String(count)} ${noun} to`}
        portfolios={choices}
        value={target.id}
        onChange={(portfolio) => {
          setPicked(portfolio);
          move.reset();
        }}
        disabled={move.isPending}
      />
      <button
        type="button"
        disabled={move.isPending}
        onClick={() => {
          move.mutate({
            portfolio_id: target.id,
            from_portfolio: scope,
            asset: filters.asset === '' ? null : filters.asset,
            venue: filters.venue === '' ? null : filters.venue,
            since: filters.from === '' ? null : startOfDay(filters.from, 0),
            until: filters.to === '' ? null : startOfDay(filters.to, 1),
          });
        }}
      >
        Move
      </button>
      {move.isSuccess && (
        <p className="note" role="status">
          Moved {move.data.moved} {move.data.moved === 1 ? 'operation' : 'operations'}.
        </p>
      )}
      {move.isError && (
        <p className="note note-error" role="alert">
          Not moved: {describeApiError(move.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
    </div>
  );
}

/**
 * The stored operations, every asset, newest first, a page at a time; four states. Filtered by
 * days, asset and venue, with the page size the owner picks; a filter that keeps nothing says
 * so beside the filters, never as "No operations yet". Narrowed to the selected portfolio, and
 * with more than one portfolio, what the filters keep can be moved to another (spec 044).
 */
function OperationsTable() {
  const { scope, portfolios } = usePortfolioScope();
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [pageSize, setPageSize] = useState<OperationsPageSize>(DEFAULT_OPERATIONS_PAGE_SIZE);
  const [page, setPage] = useState(0);
  const operations = useOperations({ portfolio: scope, page, pageSize, ...filters });
  const remove = useDeleteOperation();
  const several = portfolios.length > 1;
  // Where the move bar can send them: every portfolio but the one shown.
  const choices = portfolios.filter((portfolio) => portfolio.id !== scope);
  const [first] = choices;
  // Which portfolio each row is in, when the table shows them all.
  const named = several && scope === null ? portfolios : undefined;

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

  const { count, operations: rows, assets, venues } = operations.data;
  if (count === 0 && !filtering(filters)) {
    return (
      <EmptyState
        title="No operations yet"
        description="Upload an exchange report above, or add an operation by hand."
        headingLevel={3}
      />
    );
  }

  const pages = Math.max(1, Math.ceil(count / pageSize));
  return (
    <section
      className="card"
      aria-labelledby="operations-heading"
      aria-busy={operations.isPlaceholderData}
    >
      <div className="card-head">
        <h3 id="operations-heading">Operations</h3>
        <span className="page-meta" role="status">
          {count === 0 ? 'None match' : describeShown(page, pageSize, rows.length, count)}
        </span>
      </div>
      <FilterBar
        filters={filters}
        assets={assets}
        venues={venues}
        onChange={(next) => {
          setFilters(next);
          setPage(0);
        }}
      />
      {several && count > 0 && first !== undefined && (
        <MoveBar count={count} scope={scope} filters={filters} choices={choices} first={first} />
      )}
      {remove.isError && (
        <p className="note note-error" role="alert">
          Not deleted: {describeApiError(remove.error, WRITE_FAILURE_FALLBACK)}
        </p>
      )}
      {count === 0 ? (
        <p className="history-empty">
          No operations match these filters. Clear them, or widen the days.
        </p>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <caption className="visually-hidden">Stored operations, newest first</caption>
            <thead>
              <tr>
                <th scope="col">When</th>
                {named !== undefined && <th scope="col">Portfolio</th>}
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
                  portfolios={named}
                  deleting={remove.isPending}
                  onDelete={(id) => {
                    remove.mutate(id, {
                      // Deleting the only row of the last page empties it: step back a page.
                      onSuccess: () => {
                        if (rows.length === 1 && page > 0) {
                          setPage(page - 1);
                        }
                      },
                    });
                  }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <nav className="state-actions operations-pages" aria-label="Pages">
        <div className="field">
          <label htmlFor="operations-page-size">Rows per page</label>
          <select
            id="operations-page-size"
            value={pageSize}
            onChange={(event) => {
              setPageSize(pageSizeOf(event.target.value));
              setPage(0);
            }}
          >
            {OPERATIONS_PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
        </div>
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
    </section>
  );
}

/** A page size the select offers; anything else, which only a tampered page could send, is the
 * default. */
function pageSizeOf(value: string): OperationsPageSize {
  return (
    OPERATIONS_PAGE_SIZES.find((size) => String(size) === value) ?? DEFAULT_OPERATIONS_PAGE_SIZE
  );
}

/**
 * The Operations page (spec 042): upload the exchanges' reports, add what no report covers, and
 * every stored operation in a table. The dashboard's Invested section is built from these.
 */
export function OperationsPage() {
  const { scope } = usePortfolioScope();
  return (
    <div className="page">
      <div className="page-head">
        <h2 className="page-title">Operations</h2>
      </div>
      <div className="side-layout">
        <UploadForm />
        <ManualForm />
      </div>
      {/* Keyed by the portfolio, so another one starts again from the first page, unfiltered. */}
      <OperationsTable key={String(scope)} />
    </div>
  );
}
