import { Link } from 'react-router-dom';

import { useHealthDetail } from '@/api/health';
import { describeBackupWarning, isBackupWarning } from '@/lib/backups';

/**
 * The dashboard's warning that scheduled backups failed, stopped or cannot be checked, above
 * the value section. One `role="alert"` paragraph ending in a link to the Health page, which
 * says what happened. See docs/specs/029-sqlite-backups.md, "Design: frontend".
 *
 * It warns for `failed`, `stale` and `unreadable`. A directory that cannot be listed leaves
 * it unknown whether backups are being kept, which the owner is told rather than shown as
 * nothing (spec 029, R4). It renders nothing for `ok`, `pending` and `disabled`: a backup
 * that is working, has not run yet or is switched off is not something the owner must act
 * on. It renders nothing while the request is pending and when it failed too, since the
 * Health page is where that failure is reported, and a second place saying so would only
 * add noise to the page that matters most. The test is on `data`, not on the request's
 * status, so a poll that fails after a warning was shown leaves the warning on screen: the
 * last reading is the only one there is.
 */
export function BackupNotice() {
  const backup = useHealthDetail().data?.backup;

  if (backup === undefined || !isBackupWarning(backup.state)) {
    return null;
  }

  return (
    <p role="alert">
      {describeBackupWarning(backup.state, backup.latest_at)}{' '}
      <Link to="/health">Open backend health</Link>
    </p>
  );
}
