import type { PricesHealth } from '@/api/health';
import { NEVER_WORDS, PRICES_STATE_WORDS } from '@/lib/health';
import { HealthSection, InstantOrNone, Unavailable } from '@/pages/health/HealthSection';

const PRICES_HEADING_ID = 'prices-heading';

function PricesContent({ prices }: { readonly prices: PricesHealth }) {
  if (prices.state === 'unavailable') {
    return <Unavailable />;
  }

  return (
    <dl className="health-details">
      <dt>State</dt>
      <dd>{PRICES_STATE_WORDS[prices.state]}</dd>
      <dt>Latest fetch</dt>
      <dd>
        <InstantOrNone value={prices.latest_fetched_at} none={NEVER_WORDS} />
      </dd>
    </dl>
  );
}

/**
 * How current the prices are: `stale` once the newest one is older than the backend allows,
 * `never` before any was fetched. A price that is old is shown as old, never as a price that
 * is simply there. See docs/specs/030-observability.md.
 */
export function PricesSection({ prices }: { readonly prices: PricesHealth }) {
  return (
    <HealthSection headingId={PRICES_HEADING_ID} title="Prices">
      <PricesContent prices={prices} />
    </HealthSection>
  );
}
