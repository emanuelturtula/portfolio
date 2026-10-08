/**
 * The words for the scheduled backups' state: what the Health page says about each state and
 * each kind of failure, and the two sentences the dashboard warns with. See
 * docs/specs/029-sqlite-backups.md, "Design: frontend".
 *
 * No React anywhere in this module - it is exercised directly by tests, the same split
 * `lib/health.ts` uses for the same reason.
 *
 * Both `Record`s are keyed by the generated unions, so a state or an error kind added on the
 * backend fails `tsc` here until it has words. A failure nobody can read the meaning of is a
 * warning that does not warn.
 */
import type { BackupErrorKind, BackupState } from '@/api/health';
import { formatAbsoluteTime } from '@/lib/time';

/**
 * The state in words, for the Health page. Each one opens with a one-word label, so the
 * state reads without relying on colour or position.
 */
export const BACKUP_STATE_WORDS: Record<BackupState, string> = {
  unreadable: 'Unreadable. The backup directory cannot be read.',
  ok: 'OK. Scheduled backups are running.',
  pending: 'Pending. The first backup has not finished yet.',
  stale: 'Overdue. Scheduled backups have not completed recently.',
  failed: 'Failed. The last scheduled backup did not complete.',
  disabled: 'Disabled. Scheduled backups are switched off on this server.',
};

/** Why an attempt failed, for the Health page. The kinds are the backend's `error_kind`s. */
export const BACKUP_ERROR_WORDS: Record<BackupErrorKind, string> = {
  database_error: 'The live database could not be read.',
  integrity_failed: 'The copy failed its integrity check and was not kept.',
  storage_error:
    'The copy could not be written to storage. The disk may be full, or a permission may be missing.',
};

/** The states the dashboard warns about. `ok`, `pending` and `disabled` need no action. */
export type BackupWarningState = Extract<BackupState, 'failed' | 'stale' | 'unreadable'>;

const WARNING_STATES: ReadonlySet<BackupState> = new Set<BackupWarningState>([
  'failed',
  'stale',
  'unreadable',
]);

/** Whether the dashboard warns about `state`. */
export function isBackupWarning(state: BackupState): state is BackupWarningState {
  return WARNING_STATES.has(state);
}

/** What the Health page shows where `unreadable` leaves the newest copy and the count unknown. */
export const UNKNOWN_BACKUP_VALUE = 'unknown';

const FAILED_SENTENCE = 'The last scheduled backup failed.';
const STALE_SENTENCE = 'Scheduled backups have not completed since.';
const NO_BACKUP_SENTENCE = 'There is no backup yet.';
const UNREADABLE_SENTENCE =
  'The backup directory cannot be read, so it is not known whether backups are being kept.';

/**
 * The dashboard's warning, without its link. The date is the newest copy's instant, shown
 * with the formatter every other absolute instant uses and never as a relative phrase: the
 * sentence sits in a live region, and a ticking phrase there is re-announced each time it
 * changes (spec 016).
 *
 * | State | With a newest copy | With none |
 * |---|---|---|
 * | `failed` | "The last scheduled backup failed. The newest backup is from {date}." | "The last scheduled backup failed. There is no backup yet." |
 * | `stale` | "The newest backup is from {date}. Scheduled backups have not completed since." | "There is no backup yet." |
 * | `unreadable` | "The backup directory cannot be read, so it is not known whether backups are being kept." | the same |
 *
 * A stale state with no copy is the spec's literal wording: its second sentence is about
 * what happened "since" the date, and there is no date. `unreadable` has no newest copy to
 * name, because it is unknown rather than absent (spec 029, R4), so it never reads `latestAt`.
 */
export function describeBackupWarning(state: BackupWarningState, latestAt: string | null): string {
  if (state === 'unreadable') {
    return UNREADABLE_SENTENCE;
  }

  if (latestAt === null) {
    return state === 'failed' ? `${FAILED_SENTENCE} ${NO_BACKUP_SENTENCE}` : NO_BACKUP_SENTENCE;
  }

  const newest = `The newest backup is from ${formatAbsoluteTime(latestAt)}.`;

  return state === 'failed' ? `${FAILED_SENTENCE} ${newest}` : `${newest} ${STALE_SENTENCE}`;
}
