import { BackupNotice } from '@/pages/dashboard/BackupNotice';
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
 *
 * Above them, a warning when the scheduled backups failed or stopped, which is about the
 * data underneath rather than about either section (spec 029).
 */
export function DashboardPage() {
  return (
    <>
      <BackupNotice />
      <ValueSection />
      <InvestedSection />
    </>
  );
}
