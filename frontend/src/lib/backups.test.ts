import { describe, expect, it } from 'vitest';

import type { BackupState } from '@/api/health';
import {
  BACKUP_ERROR_WORDS,
  BACKUP_STATE_WORDS,
  describeBackupWarning,
  isBackupWarning,
  UNKNOWN_BACKUP_VALUE,
} from '@/lib/backups';
import { NEWEST_BACKUP_AT } from '@/test/backupFixtures';
import { inTimeZone } from '@/test/timeZone';

/** `Intl` separates the time from "AM" with a narrow no-break space; compare words, not bytes. */
function plain(text: string): string {
  return text.replace(/\s+/gu, ' ');
}

/** Every state the backend serves, in the order of the spec's table. */
const STATES: readonly BackupState[] = [
  'unreadable',
  'disabled',
  'failed',
  'stale',
  'pending',
  'ok',
];

describe('the words for each state', () => {
  it('has a sentence for every state, and only those', () => {
    expect(Object.keys(BACKUP_STATE_WORDS).sort()).toEqual([...STATES].sort());
  });

  it.each([
    ['unreadable', 'Unreadable. The backup directory cannot be read.'],
    ['ok', 'OK. Scheduled backups are running.'],
    ['pending', 'Pending. The first backup has not finished yet.'],
    ['stale', 'Overdue. Scheduled backups have not completed recently.'],
    ['failed', 'Failed. The last scheduled backup did not complete.'],
    ['disabled', 'Disabled. Scheduled backups are switched off on this server.'],
  ] as const)('words %s as %j', (state, words) => {
    expect(BACKUP_STATE_WORDS[state]).toBe(words);
  });

  it.each([
    ['database_error', 'The live database could not be read.'],
    ['integrity_failed', 'The copy failed its integrity check and was not kept.'],
    [
      'storage_error',
      'The copy could not be written to storage. The disk may be full, or a permission may be missing.',
    ],
  ] as const)('words the failure %s as %j', (kind, words) => {
    expect(BACKUP_ERROR_WORDS[kind]).toBe(words);
  });

  it('says "unknown" where the directory could not be read', () => {
    expect(UNKNOWN_BACKUP_VALUE).toBe('unknown');
  });
});

describe('which states the dashboard warns about', () => {
  it.each(['failed', 'stale', 'unreadable'] as const)('warns about %s', (state) => {
    expect(isBackupWarning(state)).toBe(true);
  });

  it.each(['ok', 'pending', 'disabled'] as const)('does not warn about %s', (state) => {
    expect(isBackupWarning(state)).toBe(false);
  });
});

describe('describeBackupWarning', () => {
  it('names the newest copy after a failure, with the absolute instant', () => {
    inTimeZone('UTC');

    expect(plain(describeBackupWarning('failed', NEWEST_BACKUP_AT))).toBe(
      'The last scheduled backup failed. The newest backup is from Oct 2, 2026, 3:00 AM.',
    );
  });

  it('says there is no backup yet after a failure with none', () => {
    expect(describeBackupWarning('failed', null)).toBe(
      'The last scheduled backup failed. There is no backup yet.',
    );
  });

  it('dates a stale copy and says nothing has completed since', () => {
    inTimeZone('UTC');

    expect(plain(describeBackupWarning('stale', NEWEST_BACKUP_AT))).toBe(
      'The newest backup is from Oct 2, 2026, 3:00 AM. Scheduled backups have not completed since.',
    );
  });

  it('says only that there is no backup when stale with none, as the spec words it', () => {
    expect(describeBackupWarning('stale', null)).toBe('There is no backup yet.');
  });

  it('shows the instant in the local zone, as every other absolute instant is shown', () => {
    inTimeZone('America/Argentina/Buenos_Aires');

    expect(plain(describeBackupWarning('stale', NEWEST_BACKUP_AT))).toBe(
      'The newest backup is from Oct 2, 2026, 12:00 AM. Scheduled backups have not completed since.',
    );
  });

  it.each([null, NEWEST_BACKUP_AT])(
    'says the directory cannot be read when unreadable, whatever instant it is given (%s)',
    (latestAt) => {
      expect(describeBackupWarning('unreadable', latestAt)).toBe(
        'The backup directory cannot be read, so it is not known whether backups are being kept.',
      );
    },
  );
});
