import { InvestedSection } from '@/pages/dashboard/InvestedSection';
import { ValueSection } from '@/pages/dashboard/ValueSection';

/**
 * The dashboard: what the wallets hold and what that is worth, then what went into it and
 * what it has made. See docs/specs/011-wallets-page-value-dashboard.md and
 * docs/specs/022-invested-per-asset-dashboard.md.
 *
 * Two sections rather than one page with one early return: each reads its own query and
 * owns its own loading, error and empty states, so an owner with trades and no wallets, or
 * wallets and no trades, still sees the half that has something to show.
 */
export function DashboardPage() {
  return (
    <>
      <ValueSection />
      <InvestedSection />
    </>
  );
}
