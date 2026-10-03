import type { ExchangeHealth, ExchangesHealth } from '@/api/health';
import { EmptyState } from '@/components/EmptyState';
import {
  EXCHANGE_BALANCES_WORDS,
  EXCHANGE_SYNC_WORDS,
  EXCHANGES_EMPTY_WORDS,
  exchangeName,
  NEVER_WORDS,
} from '@/lib/health';
import { HealthSection, InstantOrNone, Unavailable } from '@/pages/health/HealthSection';

const EXCHANGES_HEADING_ID = 'exchanges-heading';

function ExchangeItem({ exchange }: { readonly exchange: ExchangeHealth }) {
  return (
    <div>
      <h4>{exchangeName(exchange.exchange_key)}</h4>
      <dl className="health-details">
        <dt>Trade sync</dt>
        <dd>{EXCHANGE_SYNC_WORDS[exchange.sync_state]}</dd>
        <dt>Last successful sync</dt>
        <dd>
          <InstantOrNone value={exchange.last_synced_at} none={NEVER_WORDS} />
        </dd>
        <dt>Balances</dt>
        <dd>{EXCHANGE_BALANCES_WORDS[exchange.balances_state]}</dd>
        <dt>Balances read</dt>
        <dd>
          <InstantOrNone value={exchange.balances_read_at} none={NEVER_WORDS} />
        </dd>
      </dl>
    </div>
  );
}

function ExchangesContent({ exchanges }: { readonly exchanges: ExchangesHealth }) {
  if (exchanges.state === 'unavailable') {
    return <Unavailable />;
  }

  if (exchanges.items.length === 0) {
    return <EmptyState headingLevel={4} {...EXCHANGES_EMPTY_WORDS} />;
  }

  return exchanges.items.map((exchange) => (
    <ExchangeItem key={exchange.exchange_key} exchange={exchange} />
  ));
}

/**
 * The exchange accounts, one entry each: where the trade sync stands and, separately, where
 * the balance read stands, with the instant of each. The two fail independently - a balance
 * can be unreadable while the trades sync - so neither is inferred from the other. See
 * docs/specs/030-observability.md.
 */
export function ExchangesSection({ exchanges }: { readonly exchanges: ExchangesHealth }) {
  return (
    <HealthSection headingId={EXCHANGES_HEADING_ID} title="Exchanges">
      <ExchangesContent exchanges={exchanges} />
    </HealthSection>
  );
}
