import { describeApiError } from '@/api/client';
import { useHealthDetail } from '@/api/health';
import { ErrorState } from '@/components/ErrorState';
import { ChainsSection } from '@/pages/health/ChainsSection';
import { PricesSection } from '@/pages/health/PricesSection';
import { TimersSection } from '@/pages/health/TimersSection';

const UNREACHABLE_FALLBACK =
  'The backend could not be reached. Check that the API is running, then reload the page.';

/**
 * Everything on the Health page after Backups: the timers, the balance sync per chain and the
 * prices. They read the same entry as Backups,
 * `GET /api/health/detail` through `useHealthDetail()`, so nothing new is fetched. See
 * docs/specs/030-observability.md, "Design: frontend".
 *
 * It renders the query's three states once for all of them, not once per section: a request
 * that did not answer would otherwise print the same alert three times. Backups reports the
 * same failure in its own words, so there are two alerts, each true of what it names.
 *
 * - pending: one announced loading line;
 * - failed: one alert, headed `h3` like the sections it stands in for (an `h4` right after
 *   Backups would read as Backups' child), and none of the sections - nothing is shown as
 *   `ok`, or as empty, on the strength of a request that did not answer;
 * - success: each section, which tells apart its own empty, `unavailable` and populated
 *   states. A section the backend could not build is `unavailable` and the others still
 *   answer.
 */
export function DetailSections() {
  const { data, error, isPending, isError } = useHealthDetail();

  if (isPending) {
    return (
      <p className="state" role="status">
        Loading the timers and sources...
      </p>
    );
  }

  if (isError) {
    return (
      <ErrorState
        headingLevel={3}
        title="Could not load the timers and sources"
        description={describeApiError(error, UNREACHABLE_FALLBACK)}
      />
    );
  }

  return (
    <>
      <TimersSection timers={data.schedulers} />
      <ChainsSection chains={data.chains} />
      <PricesSection prices={data.prices} />
    </>
  );
}
