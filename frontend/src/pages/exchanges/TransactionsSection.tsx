import { useSearchParams } from 'react-router-dom';
import type { UseQueryResult } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import { useExchangeFills, type Exchange, type ExchangeFillsPage } from '@/api/exchanges';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { EXCHANGES } from '@/lib/exchanges';
import {
  isInvertedRange,
  NO_FILTERS,
  readFillFilters,
  writeFillFilters,
  type FillFilters,
} from '@/lib/fillFilters';
import {
  describeEmptyFills,
  describeFillScope,
  emptyFillsWords,
  pageCount,
  type EmptyFills,
} from '@/lib/fills';
import { CompletenessNotice } from '@/pages/exchanges/CompletenessNotice';
import { FillFiltersForm } from '@/pages/exchanges/FillFiltersForm';
import { FillTable } from '@/pages/exchanges/FillTable';
import { FillTotalsView } from '@/pages/exchanges/FillTotalsView';
import { Pagination } from '@/pages/exchanges/Pagination';

const FILLS_LOAD_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const FILLS_REFETCH_FALLBACK = 'The server could not be reached.';

interface EmptyFillsStateProps {
  readonly state: EmptyFills;
  readonly onClear: () => void;
}

/**
 * The three ways a query can match nothing, told apart (spec 024). A failing sync is an
 * `ErrorState` with `role="alert"`: "nothing here" and "the sync is failing" mean opposite
 * things, and an assertive announcement is what stops the second being read as the first. The
 * other two are honest absences, and only "nothing matches" has a way out.
 */
function EmptyFillsState({ state, onClear }: EmptyFillsStateProps) {
  const { title, description } = emptyFillsWords(state);

  switch (state.kind) {
    case 'sync_failing':
      return (
        <ErrorState
          headingLevel={4}
          title={title}
          description={description}
          action={state.venues.map((venue) => (
            <p key={venue}>
              <a href={`#exchange-${venue}`}>See the {EXCHANGES[venue].name} account</a>
            </p>
          ))}
        />
      );
    case 'no_match':
      return (
        <EmptyState
          headingLevel={4}
          title={title}
          description={description}
          action={
            <button type="button" onClick={onClear}>
              Clear filters
            </button>
          }
        />
      );
    case 'none_imported':
      return <EmptyState headingLevel={4} title={title} description={description} />;
  }
}

interface FillsViewProps {
  readonly data: ExchangeFillsPage;
  readonly filters: FillFilters;
  readonly page: number;
  readonly exchanges: readonly Exchange[] | undefined;
  /** The rows are the previous page's, until the next arrives. */
  readonly busy: boolean;
  readonly onPageChange: (page: number) => void;
}

/**
 * The scope, the completeness notice, the totals and one page of rows. The totals are the
 * whole filtered set's and do not depend on the page.
 *
 * A page past the end can only come from a URL edited by hand or a link that outlived its
 * data (the fills are append-only, so the count only grows). The API answers it with no rows
 * and the same totals, and "Showing 6,001 to 812 of 812" would be nonsense, so it says so and
 * offers the last page instead.
 */
function FillsView({ data, filters, page, exchanges, busy, onPageChange }: FillsViewProps) {
  const pages = pageCount(data.total_count);

  return (
    <>
      <p>{describeFillScope(data.total_count, filters)}</p>
      <CompletenessNotice exchanges={exchanges} filters={filters} />
      <FillTotalsView totals={data.totals} />

      <h4 id="fills-heading">Fills</h4>
      {page > pages ? (
        <>
          <p>
            There is no page {page}: the last page is {pages}.
          </p>
          <button
            type="button"
            onClick={() => {
              onPageChange(pages);
            }}
          >
            Go to the last page
          </button>
        </>
      ) : (
        <>
          <div aria-busy={busy}>
            <FillTable fills={data.fills} />
          </div>
          <Pagination page={page} total={data.total_count} onPageChange={onPageChange} />
        </>
      )}
    </>
  );
}

interface FillResultsProps {
  readonly fills: UseQueryResult<ExchangeFillsPage>;
  readonly filters: FillFilters;
  readonly page: number;
  readonly exchanges: readonly Exchange[] | undefined;
  readonly onPageChange: (page: number) => void;
  readonly onClear: () => void;
}

/**
 * The four states of the fills request: loading, error, empty and success. Under new filters
 * there is nothing to show yet, and a skeleton is what stands there rather than rows that
 * were asked for under other filters.
 */
function FillResults({ fills, filters, page, exchanges, onPageChange, onClear }: FillResultsProps) {
  if (fills.isPending) {
    return <Skeleton label="Loading transactions…" />;
  }

  // Only when nothing has loaded for these filters: a refresh that fails after a good load
  // leaves the last reading in `data`, and blanking rows the owner is reading for one missed
  // refetch is worse than leaving them (see `isRefetchError` below).
  if (fills.isLoadingError) {
    return (
      <ErrorState
        headingLevel={4}
        title="Could not load transactions"
        description={describeApiError(fills.error, FILLS_LOAD_FALLBACK)}
        onRetry={() => {
          void fills.refetch();
        }}
      />
    );
  }

  const data = fills.data;

  return (
    <>
      {fills.isRefetchError && (
        <p role="alert">
          Could not refresh transactions: {describeApiError(fills.error, FILLS_REFETCH_FALLBACK)}{' '}
          Showing what was last loaded.
        </p>
      )}
      {data.total_count === 0 ? (
        <>
          <CompletenessNotice exchanges={exchanges} filters={filters} />
          <EmptyFillsState state={describeEmptyFills(filters, exchanges)} onClear={onClear} />
        </>
      ) : (
        <FillsView
          data={data}
          filters={filters}
          page={page}
          exchanges={exchanges}
          busy={fills.isPlaceholderData}
          onPageChange={onPageChange}
        />
      )}
    </>
  );
}

interface TransactionsSectionProps {
  /** The exchange list. `undefined` is a list that could not be read: completeness is unknown. */
  readonly exchanges: readonly Exchange[] | undefined;
}

/**
 * Every imported trade, with exchange and day filters and what they add up to. See
 * docs/specs/024-exchange-transactions.md.
 *
 * **The filters live in the URL** (`exchange`, `from`, `to`, `page`), so a reload and
 * back/forward restore them. Changing a filter returns to page 1; Clear filters removes all
 * four. Days are local days, and the form states the zone used.
 *
 * **Independent of Accounts.** This section has its own request and its own four states, and
 * reads the exchange list only to say what may be missing (`exchanges`). A failed fills
 * request does not blank Accounts, and a failed list does not blank this.
 *
 * **An inverted range is refused, not sent.** The form says so, and the request is off.
 */
export function TransactionsSection({ exchanges }: TransactionsSectionProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const { filters, page } = readFillFilters(searchParams);
  const inverted = isInvertedRange(filters);
  const fills = useExchangeFills(filters, page);

  function show(nextFilters: FillFilters, nextPage: number): void {
    setSearchParams(writeFillFilters(nextFilters, nextPage));
  }

  function clear(): void {
    show(NO_FILTERS, 1);
  }

  return (
    <section aria-labelledby="transactions-heading">
      <h3 id="transactions-heading">Transactions</h3>
      <FillFiltersForm
        filters={filters}
        inverted={inverted}
        onChange={(next) => {
          show(next, 1);
        }}
        onClear={clear}
      />
      {!inverted && (
        <FillResults
          fills={fills}
          filters={filters}
          page={page}
          exchanges={exchanges}
          onPageChange={(nextPage) => {
            show(filters, nextPage);
          }}
          onClear={clear}
        />
      )}
    </section>
  );
}
