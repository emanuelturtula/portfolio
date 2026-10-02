import { usePositions } from '@/api/accounting';
import { failedRecompute } from '@/lib/accounting';
import { FailedAttempt } from '@/pages/dashboard/InvestedSection';

/**
 * Says so when the last recompute of the positions failed: a change recorded here is saved
 * either way, and the recompute that would show it on the dashboard is the one that failed, so
 * the dashboard's figures are from before it. The wording and the absolute instant are the
 * dashboard's own (`FailedAttempt`), because this is the same fact.
 *
 * The page reads the positions for this and nothing else, and its own job does not depend on
 * them: while the query is pending, or has nothing to show because it failed, this renders
 * nothing - a failed read of a warning is not a warning, and it is not "all is well" either,
 * but the form and the list must work without it.
 */
export function FailedRecomputeAlert() {
  const positions = usePositions();
  const failed =
    positions.data === undefined ? null : failedRecompute(positions.data.last_recompute);

  if (failed === null) {
    return null;
  }

  return (
    <p role="alert">
      The last recompute of the positions <FailedAttempt at={failed.at} error={failed.error} />.
      Changes made here are saved, but the dashboard&apos;s figures are from before it.
    </p>
  );
}
