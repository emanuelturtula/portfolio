import { useState } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import {
  DEFAULT_HISTORY_RANGE,
  HISTORY_RANGES,
  usePortfolioHistory,
  type HistoryRange,
} from '@/api/history';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import {
  PORTFOLIO_EMPTY_WORDS,
  RANGE_LABELS,
  RANGE_PHRASES,
  type HistoryPoint,
} from '@/lib/history';
import { ValueHistoryChart } from '@/pages/dashboard/ValueHistoryChart';

export const HISTORY_LOADING_LABEL = 'Loading the value history…';

/** For a first load that got nothing back, as the summary's own failure is worded. */
const LOAD_FAILURE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const REFETCH_FAILURE_FALLBACK = 'The server could not be reached.';

interface RangeSelectorProps {
  readonly value: HistoryRange;
  readonly onChange: (range: HistoryRange) => void;
}

/** How far back the chart reaches: a row of toggle buttons, the chosen one pressed. */
export function RangeSelector({ value, onChange }: RangeSelectorProps) {
  return (
    <div className="range-selector" role="group" aria-label="Range">
      {HISTORY_RANGES.map((range) => (
        <button
          key={range}
          type="button"
          aria-pressed={range === value}
          onClick={() => {
            onChange(range);
          }}
        >
          {RANGE_LABELS[range]}
        </button>
      ))}
    </div>
  );
}

/** The heading of a value-history card, with the unit every value on it is in. */
export function HistoryTitle({ id, children }: { readonly id: string; readonly children: string }) {
  return (
    <div className="history-title">
      <h2 id={id}>{children}</h2>
      <span className="page-meta">USDT</span>
    </div>
  );
}

/** What either endpoint answers, as far as the chart reads it. */
interface HistoryData {
  readonly range: HistoryRange;
  readonly points: readonly HistoryPoint[];
}

interface HistoryViewProps {
  readonly query: UseQueryResult<HistoryData>;
  /** What is drawn, for the chart's accessible name: "Portfolio value". */
  readonly subject: string;
  readonly color?: string | undefined;
  readonly asset?: string | undefined;
  readonly emptyText: string;
}

/**
 * A value history's four states: loading, announced; a failure, in an alert with a retry;
 * nothing valued yet, in a sentence; and the chart.
 *
 * A failed poll after a good load keeps the chart, with a notice, as the dashboard keeps its
 * figures. While a newly chosen range loads, the previous one stays on screen, dimmed and
 * `aria-busy`, and the accessible name keeps saying which range is drawn.
 */
export function HistoryView({ query, subject, color, asset, emptyText }: HistoryViewProps) {
  if (query.isPending) {
    return <Skeleton label={HISTORY_LOADING_LABEL} />;
  }

  if (query.isLoadingError) {
    return (
      <ErrorState
        title="Could not load the value history"
        headingLevel={3}
        description={describeApiError(query.error, LOAD_FAILURE_FALLBACK)}
        onRetry={() => {
          void query.refetch();
        }}
      />
    );
  }

  const data = query.data;

  return (
    <>
      {query.isRefetchError && (
        <p className="note note-error" role="alert">
          Could not refresh the value history:{' '}
          {describeApiError(query.error, REFETCH_FAILURE_FALLBACK)} Showing what was last loaded.
        </p>
      )}
      <ValueHistoryChart
        points={data.points}
        caption={`${subject}, ${RANGE_PHRASES[data.range]}`}
        color={color}
        asset={asset}
        emptyText={emptyText}
        busy={query.isPlaceholderData}
      />
    </>
  );
}

/**
 * The dashboard's chart (spec 037): what every active wallet together was worth on each day of
 * the chosen range, 90 days to begin with.
 */
export function PortfolioHistory() {
  const [range, setRange] = useState<HistoryRange>(DEFAULT_HISTORY_RANGE);
  const history = usePortfolioHistory(range);

  return (
    <section className="card history-card" aria-labelledby="portfolio-history-heading">
      <div className="card-head">
        <HistoryTitle id="portfolio-history-heading">Value over time</HistoryTitle>
        <RangeSelector value={range} onChange={setRange} />
      </div>
      <HistoryView
        query={history}
        subject="Portfolio value"
        color="var(--series-blue)"
        emptyText={PORTFOLIO_EMPTY_WORDS}
      />
    </section>
  );
}
