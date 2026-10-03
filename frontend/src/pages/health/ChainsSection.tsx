import type { ChainHealth, ChainsHealth } from '@/api/health';
import { EmptyState } from '@/components/EmptyState';
import { chainDisplayName } from '@/lib/chains';
import { SYNC_ERROR_MESSAGES } from '@/lib/freshness';
import { CHAIN_STATE_WORDS, CHAINS_EMPTY_WORDS, NEVER_WORDS } from '@/lib/health';
import { HealthSection, InstantOrNone, Unavailable } from '@/pages/health/HealthSection';

const CHAINS_HEADING_ID = 'chains-heading';

function ChainItem({ chain }: { readonly chain: ChainHealth }) {
  return (
    <div>
      <h4>{chainDisplayName(chain.chain_key)}</h4>
      <dl className="health-details">
        <dt>State</dt>
        <dd>{CHAIN_STATE_WORDS[chain.state]}</dd>
        <dt>Last success</dt>
        <dd>
          <InstantOrNone value={chain.last_success_at} none={NEVER_WORDS} />
        </dd>
        {chain.last_error_kind !== null && (
          <>
            <dt>Last failure</dt>
            <dd>{SYNC_ERROR_MESSAGES[chain.last_error_kind]}</dd>
          </>
        )}
      </dl>
    </div>
  );
}

function ChainsContent({ chains }: { readonly chains: ChainsHealth }) {
  if (chains.state === 'unavailable') {
    return <Unavailable />;
  }

  if (chains.items.length === 0) {
    return <EmptyState headingLevel={4} {...CHAINS_EMPTY_WORDS} />;
  }

  return chains.items.map((chain) => <ChainItem key={chain.chain_key} chain={chain} />);
}

/**
 * The balance sync, one entry per chain: whether the last finished run read it, when one last
 * did, and why the last one did not. What the last attempt said is all there is - no provider
 * is called to ask. A section that could not be read is an alert, never an empty list, and a
 * list with nothing in it is told apart from both. See docs/specs/030-observability.md.
 */
export function ChainsSection({ chains }: { readonly chains: ChainsHealth }) {
  return (
    <HealthSection headingId={CHAINS_HEADING_ID} title="Balance sync">
      <ChainsContent chains={chains} />
    </HealthSection>
  );
}
