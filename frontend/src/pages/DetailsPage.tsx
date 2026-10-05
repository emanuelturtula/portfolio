import { InvestedSection } from '@/pages/dashboard/InvestedSection';
import { ValueSection } from '@/pages/dashboard/ValueSection';

/**
 * Everything behind the dashboard's figures: what each wallet holds and what it is worth, the
 * sync state, and what went into each position and what it has made. See
 * docs/specs/011-wallets-page-value-dashboard.md and
 * docs/specs/022-invested-per-asset-dashboard.md.
 *
 * This was the dashboard until #154 gave the dashboard three figures, the holdings and their
 * distribution. Nothing here changed in the move: it is the same two sections, reached from the
 * header's Details link, for when the summary's figures need explaining.
 *
 * Two sections rather than one page with one early return: each reads its own query and
 * owns its own loading, error and empty states, so an owner with trades and no wallets, or
 * wallets and no trades, still sees the half that has something to show.
 */
export function DetailsPage() {
  return (
    <>
      <ValueSection />
      <InvestedSection />
    </>
  );
}
