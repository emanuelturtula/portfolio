import { useState } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';

import { describeApiError } from '@/api/client';
import {
  DEFAULT_HISTORY_RANGE,
  HISTORY_RANGES,
  usePortfolioHistory,
  type HistoryRange,
} from '@/api/history';
import { useInvestment } from '@/api/operations';
import { ErrorState } from '@/components/ErrorState';
import { Skeleton } from '@/components/Skeleton';
import { assetColors, colorOf } from '@/lib/assetColors';
import {
  PORTFOLIO_EMPTY_WORDS,
  portfolioSeries,
  RANGE_LABELS,
  RANGE_PHRASES,
  TOTAL_COLOR,
  TOTAL_SERIES,
  type ChartSeries,
  type HistoryPoint,
} from '@/lib/history';
import { investedPoints } from '@/lib/investment';
import { ValueHistoryChart } from '@/pages/dashboard/ValueHistoryChart';

/** The invested line's colour: the neutral grey, apart from the total's blue and every asset's. */
export const INVESTED_COLOR = 'var(--series-other)';

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

/** One line over the answer's own points, for a chart of one thing: a wallet. */
export function singleSeries(label: string, color: string): (data: HistoryData) => ChartSeries[] {
  return (data) => [{ key: 'value', label, color, points: data.points }];
}

interface HistoryViewProps<T extends HistoryData> {
  readonly query: UseQueryResult<T>;
  /** What is drawn, for the chart's accessible name: "Portfolio value". */
  readonly subject: string;
  /** The lines to draw from what the endpoint answered. */
  readonly series: (data: T) => readonly ChartSeries[];
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
export function HistoryView<T extends HistoryData>({
  query,
  subject,
  series,
  asset,
  emptyText,
}: HistoryViewProps<T>) {
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
        series={series(data)}
        caption={`${subject}, ${RANGE_PHRASES[data.range]}`}
        asset={asset}
        emptyText={emptyText}
        busy={query.isPlaceholderData}
      />
    </>
  );
}

interface SeriesSelectorProps {
  /** Every asset the history carries, in the order served. */
  readonly assets: readonly string[];
  readonly colors: ReadonlyMap<string, string>;
  readonly value: readonly string[];
  readonly onChange: (selected: readonly string[]) => void;
}

/**
 * Which lines the chart draws (spec 041): the total and each asset, as toggle buttons, any
 * number of them pressed. The last one pressed cannot be released, so the chart always draws
 * something; it is disabled rather than silently ignoring the click. Each button carries its
 * line's colour as a swatch, so the row is the chart's legend too.
 */
export function SeriesSelector({ assets, colors, value, onChange }: SeriesSelectorProps) {
  const options = [
    { key: TOTAL_SERIES, label: 'Total', color: TOTAL_COLOR },
    ...assets.map((asset) => ({ key: asset, label: asset, color: colorOf(colors, asset) })),
  ];
  return (
    <div className="range-selector series-selector" role="group" aria-label="Series">
      {options.map((option) => {
        const on = value.includes(option.key);
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={on}
            disabled={on && value.length === 1}
            onClick={() => {
              onChange(
                on
                  ? value.filter((key) => key !== option.key)
                  : options
                      .map(({ key }) => key)
                      .filter((key) => key === option.key || value.includes(key)),
              );
            }}
          >
            <span
              className="series-swatch"
              style={{ backgroundColor: option.color }}
              aria-hidden="true"
            />
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * The dashboard's chart (spec 037): what every active wallet together was worth on each day of
 * the chosen range, 90 days to begin with. Spec 041 adds each asset as a line of its own,
 * chosen beside the total; the total alone is the default.
 *
 * A chosen asset the history no longer carries (its last wallet archived) is dropped, and when
 * nothing chosen is left the total is drawn, so the chart is never empty by selection.
 *
 * Spec 042 adds what was invested, a step line over the same days, shown by default once any
 * operation has been uploaded and toggled beside the series.
 */
export function PortfolioHistory() {
  const [range, setRange] = useState<HistoryRange>(DEFAULT_HISTORY_RANGE);
  const [chosen, setChosen] = useState<readonly string[]>([TOTAL_SERIES]);
  const [showInvested, setShowInvested] = useState(true);
  const history = usePortfolioHistory(range);
  const steps = useInvestment().data?.invested_by_day ?? [];
  const assets = history.data?.assets ?? [];
  const colors = assetColors(assets);
  const kept = chosen.filter((key) => key === TOTAL_SERIES || assets.includes(key));
  const selected = kept.length > 0 ? kept : [TOTAL_SERIES];

  return (
    <section className="card history-card" aria-labelledby="portfolio-history-heading">
      <div className="card-head">
        <HistoryTitle id="portfolio-history-heading">Value over time</HistoryTitle>
        <RangeSelector value={range} onChange={setRange} />
      </div>
      {assets.length > 0 && (
        <SeriesSelector assets={assets} colors={colors} value={selected} onChange={setChosen} />
      )}
      {steps.length > 0 && (
        <div className="range-selector series-selector" role="group" aria-label="Compare">
          <button
            type="button"
            aria-pressed={showInvested}
            onClick={() => {
              setShowInvested(!showInvested);
            }}
          >
            <span
              className="series-swatch"
              style={{ backgroundColor: INVESTED_COLOR }}
              aria-hidden="true"
            />
            Invested
          </button>
        </div>
      )}
      <HistoryView
        query={history}
        subject="Portfolio value"
        series={(data) => [
          ...portfolioSeries(data, selected, colors),
          ...(showInvested && steps.length > 0
            ? [
                {
                  key: 'invested',
                  label: 'Invested',
                  color: INVESTED_COLOR,
                  points: investedPoints(
                    data.points.map((point) => point.day),
                    steps,
                  ),
                },
              ]
            : []),
        ]}
        emptyText={PORTFOLIO_EMPTY_WORDS}
      />
    </section>
  );
}
