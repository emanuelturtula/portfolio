import { Fragment } from 'react';
import { Link } from 'react-router-dom';

import type { ReconciliationHealth } from '@/api/health';
import { HOLDINGS_CHECK_ID } from '@/lib/accounting';
import {
  NEVER_WORDS,
  RECONCILIATION_LINK_WORDS,
  RECONCILIATION_STATE_WORDS,
  reconciliationCounts,
} from '@/lib/health';
import { HealthSection, InstantOrNone, Unavailable } from '@/pages/health/HealthSection';

const RECONCILIATION_HEADING_ID = 'reconciliation-heading';

function ReconciliationContent({
  reconciliation,
}: {
  readonly reconciliation: ReconciliationHealth;
}) {
  if (reconciliation.state === 'unavailable') {
    return <Unavailable />;
  }

  return (
    <dl className="health-details">
      <dt>State</dt>
      <dd>{RECONCILIATION_STATE_WORDS[reconciliation.state]}</dd>
      <dt>Computed</dt>
      <dd>
        <InstantOrNone value={reconciliation.computed_at} none={NEVER_WORDS} />
      </dd>
      {reconciliationCounts(reconciliation).map(({ label, value }) => (
        <Fragment key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </Fragment>
      ))}
    </dl>
  );
}

/**
 * The holdings check, summarised: its state, when it was computed, and how many assets it
 * compared, how many differ and how many sources it could not compare. No quantity, asset or
 * tolerance is served here; the link goes to the holdings check on the dashboard, which has
 * them. The link stays whatever the state, since the dashboard reads its own endpoint.
 * See docs/specs/030-observability.md.
 */
export function ReconciliationSection({
  reconciliation,
}: {
  readonly reconciliation: ReconciliationHealth;
}) {
  return (
    <HealthSection headingId={RECONCILIATION_HEADING_ID} title="Reconciliation">
      <ReconciliationContent reconciliation={reconciliation} />
      <p>
        <Link to={{ pathname: '/', hash: `#${HOLDINGS_CHECK_ID}` }}>
          {RECONCILIATION_LINK_WORDS}
        </Link>
      </p>
    </HealthSection>
  );
}
