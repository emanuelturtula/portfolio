import { EmptyState } from '@/components/EmptyState';

/**
 * Placeholder for the real dashboard (#11: wallets and portfolio value; #20:
 * invested-per-asset). This change's only job is the shell around it, so
 * there is nothing here to query yet - just the honest "nothing here" state,
 * not a fabricated zero.
 */
export function DashboardPage() {
  return (
    <EmptyState
      title="No wallets yet"
      description="Add a wallet to start tracking its balance and value."
    />
  );
}
