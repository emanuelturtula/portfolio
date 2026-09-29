import { Link } from 'react-router-dom';
import type { UseQueryResult } from '@tanstack/react-query';

import { usePositions, type Positions } from '@/api/accounting';
import { describeApiError } from '@/api/client';
import { useExchanges, type Exchange } from '@/api/exchanges';
import { AbsoluteTime } from '@/components/AbsoluteTime';
import { EmptyState } from '@/components/EmptyState';
import { ErrorState } from '@/components/ErrorState';
import { RelativeTime } from '@/components/RelativeTime';
import { Skeleton } from '@/components/Skeleton';
import {
  describeClosedPositions,
  describeEmptyPositions,
  flagsOf,
  formatVenues,
  isHeld,
  venuesWithFailedSync,
  type EmptyPositions,
} from '@/lib/accounting';
import type { ExchangeKey } from '@/lib/exchanges';
import { FlagLegend } from '@/pages/dashboard/FlagLegend';
import { HistoryWarnings } from '@/pages/dashboard/HistoryWarnings';
import { InvestedSummary } from '@/pages/dashboard/InvestedSummary';
import { PositionTable } from '@/pages/dashboard/PositionTable';

const POSITIONS_LOAD_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';
const POSITIONS_REFETCH_FALLBACK = 'The server could not be reached.';
/**
 * Complete sentence, like the value section's fallbacks: a real `problem.detail` is one, and
 * the sentence that follows it carries no full stop of its own to double up.
 */
const EXCHANGES_UNAVAILABLE_FALLBACK = 'The exchange list could not be read.';

const LOADING_LABEL = 'Loading invested per asset…';

const EXCHANGES_LINK = <Link to="/exchanges">Open exchanges</Link>;

/**
 * "failed on Sep 29, 2026, 10:00 AM (UnconvertibleFillError)". The instant is absolute, not
 * a `<RelativeTime>`: both callers sit in a `role="alert"`, and a ticking phrase inside a
 * live region is re-announced every time it changes (spec 016). `error` is the exception's
 * class name, never its message. `last_recompute` lives in memory and the trigger records the
 * class name for every failure, so a null one is a shape the backend cannot write: the type
 * allows it, and it renders nothing rather than an invented name (spec 022, R1).
 */
function FailedAttempt({ at, error }: { readonly at: string; readonly error: string | null }) {
  return (
    <>
      failed on <AbsoluteTime value={at} />
      {error !== null && <> ({error})</>}
    </>
  );
}

/**
 * What "no trades imported yet" adds: whether syncing can help. `undefined` is an exchange
 * list that could not be read, where nothing can be said about what is configured.
 */
function noTradesDescription(anyConfigured: boolean | undefined): string {
  if (anyConfigured === undefined) {
    return 'Positions appear here once trades are imported from an exchange.';
  }

  return anyConfigured
    ? 'Syncing the exchanges imports your trades.'
    : 'An exchange must be configured on the server before trades can be imported.';
}

/**
 * The empty response, told apart four ways. Two are failures and render as an `ErrorState`
 * (`role="alert"`): "the recompute failed" and "the sync failed" mean the opposite of "there
 * is nothing", and an assertive announcement is what stops them being read as it. The other
 * three are honest absences and render as an `EmptyState`.
 */
function EmptyPositionsState({ state }: { readonly state: EmptyPositions }) {
  switch (state.kind) {
    case 'recompute_failed':
      return (
        <ErrorState
          headingLevel={3}
          title="Positions could not be computed"
          description={
            <>
              The last attempt <FailedAttempt at={state.at} error={state.error} />. It is tried
              again after the next exchange sync.
            </>
          }
          action={EXCHANGES_LINK}
        />
      );
    case 'sync_failed':
      return (
        <ErrorState
          headingLevel={3}
          title="The exchange sync failed"
          description={
            `Trades from ${formatVenues(state.venues)} may be missing: the last sync failed. ` +
            'Positions are computed from imported trades.'
          }
          action={EXCHANGES_LINK}
        />
      );
    case 'no_trades':
      return (
        <EmptyState
          headingLevel={3}
          title="No trades imported yet"
          description={noTradesDescription(state.anyConfigured)}
          action={EXCHANGES_LINK}
        />
      );
    case 'not_computed':
      return (
        <EmptyState
          headingLevel={3}
          title="Positions have not been computed yet"
          description="They are computed at startup and after each exchange sync that stores a trade."
        />
      );
    case 'no_positions':
      return (
        <EmptyState
          headingLevel={3}
          title="No positions"
          description="Every trade imported so far is between stablecoins, which are held at cost."
        />
      );
  }
}

interface PositionsViewProps {
  readonly data: Positions;
  /** When the snapshot was written. The section shows positions only for a written one. */
  readonly computedAt: string;
  /** Venues whose last sync failed, so these figures may miss their latest trades. */
  readonly failedVenues: readonly ExchangeKey[];
}

/**
 * The section when there are positions: how the figures were computed, what to distrust
 * about them, the summary, the held positions and what the history could not account for.
 */
function PositionsView({ data, computedAt, failedVenues }: PositionsViewProps) {
  const held = data.positions.filter(isHeld);
  const closed = data.positions.filter((position) => !isHeld(position));

  return (
    <>
      <p>
        Weighted average cost in {data.quote_currency}, computed <RelativeTime value={computedAt} />
        . Not a tax figure.
      </p>

      {data.last_recompute?.outcome === 'failed' && (
        <p role="alert">
          The last recompute{' '}
          <FailedAttempt at={data.last_recompute.at} error={data.last_recompute.error} />. These
          figures were computed before that.
        </p>
      )}
      {failedVenues.length > 0 && (
        <p role="alert">
          These figures may miss recent trades from {formatVenues(failedVenues)}: the last sync
          failed. {EXCHANGES_LINK}
        </p>
      )}

      <InvestedSummary
        totals={data.totals}
        positions={data.positions}
        quoteCurrency={data.quote_currency}
        unallocatedCosts={data.unallocated_costs}
      />
      <PositionTable
        positions={held}
        excluded={data.totals.excluded}
        quoteCurrency={data.quote_currency}
      />
      {closed.length > 0 && <p>{describeClosedPositions(closed)}</p>}
      <FlagLegend flags={flagsOf(data.positions)} />
      {data.warnings.length > 0 && <HistoryWarnings warnings={data.warnings} />}
    </>
  );
}

interface InvestedContentProps {
  readonly positions: UseQueryResult<Positions>;
  readonly exchanges: UseQueryResult<Exchange[]>;
}

function InvestedContent({ positions, exchanges }: InvestedContentProps) {
  // Both "loading" cases return this same element from the same place, so React keeps one
  // `role="status"` region across the hand-over from the first to the second instead of
  // removing it and adding another, which a screen reader announces twice (spec 022, N5).
  if (positions.isPending) {
    return <Skeleton label={LOADING_LABEL} />;
  }

  // Whole-section only when there is nothing to show at all - see `isLoadingError` in the
  // value section: a background poll that fails after a successful load leaves the last
  // good reading in `data`, and blanking a loaded section for one missed poll is worse than
  // leaving it stale.
  if (positions.isLoadingError) {
    return (
      <ErrorState
        headingLevel={3}
        title="Could not load invested per asset"
        description={describeApiError(positions.error, POSITIONS_LOAD_FALLBACK)}
        onRetry={() => {
          void positions.refetch();
        }}
      />
    );
  }

  const data = positions.data;
  // `undefined` for a failed exchanges query, which `describeEmptyPositions` reads as "cannot
  // tell": a stale list kept across a failed poll is no more trusted than none, the same
  // rule the value section applies to the run log (`runsKnown`).
  const exchangeList = exchanges.isSuccess ? exchanges.data : undefined;
  const failedVenues = exchangeList === undefined ? [] : venuesWithFailedSync(exchangeList);
  const computedAt = data.computed_at;
  // No snapshot is `computed_at === null` by the endpoint's own definition, and it comes with
  // no positions (spec 021), so the two cannot disagree. Narrowing here, once, is what lets
  // the positions path take `computedAt` as a string instead of carrying a fallback for a
  // response the backend cannot write (spec 022, R1).
  const empty = computedAt === null || data.positions.length === 0;

  // Which of the empty states applies depends on the exchanges query, so until it has
  // answered the section cannot tell "no trades yet" from "the sync failed".
  if (empty && exchanges.isPending) {
    return <Skeleton label={LOADING_LABEL} />;
  }

  return (
    <>
      {positions.isError && (
        <p role="alert">
          Could not refresh invested per asset:{' '}
          {describeApiError(positions.error, POSITIONS_REFETCH_FALLBACK)} Showing what was last
          loaded.
        </p>
      )}
      {exchanges.isError && (
        <p role="alert">
          Exchange status is unavailable:{' '}
          {describeApiError(exchanges.error, EXCHANGES_UNAVAILABLE_FALLBACK)} A failed exchange sync
          cannot be ruled out.
        </p>
      )}

      {empty ? (
        <EmptyPositionsState state={describeEmptyPositions(data, exchangeList)} />
      ) : (
        <PositionsView data={data} computedAt={computedAt} failedVenues={failedVenues} />
      )}
    </>
  );
}

/**
 * What went into the portfolio and what it has made: invested per asset, average cost,
 * market value and unrealized P&L, from `GET /api/accounting/positions`. See
 * docs/specs/022-invested-per-asset-dashboard.md.
 *
 * Independent of the value section above it: its own query, its own loading, error and
 * empty states. It also reads the exchanges list - the Exchanges page's own query and cache
 * - but only to tell "no trades imported yet" from "the sync failed" and to warn that a
 * failed sync may have left trades out.
 */
export function InvestedSection() {
  const positions = usePositions();
  const exchanges = useExchanges(false);

  return (
    <section aria-labelledby="invested-heading">
      <h2 id="invested-heading">Invested</h2>
      <InvestedContent positions={positions} exchanges={exchanges} />
    </section>
  );
}
