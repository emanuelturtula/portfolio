import { http, HttpResponse, type HttpHandler } from 'msw';

import type { BackupStatus, HealthDetail } from '@/api/health';

/**
 * `GET /api/health/detail` as the backend serves it (spec 029), one fixture per state. The
 * instants are the spec's own example, in UTC with a `Z`, as the API serialises them.
 */
export const HEALTH_DETAIL_PATH = '/api/health/detail';

/** The newest copy in every fixture that has one: 2026-10-02 at 03:00 UTC. */
export const NEWEST_BACKUP_AT = '2026-10-02T03:00:00.123456Z';

/** When the failed attempt in the failed fixtures started: 2026-10-03 at 03:00 UTC. */
export const FAILED_ATTEMPT_AT = '2026-10-03T03:00:00.654321Z';

/** A status with every field given, `ok` with nine copies, as the spec's example. */
export function backupStatus(overrides: Partial<BackupStatus> = {}): BackupStatus {
  return {
    state: 'ok',
    latest_at: NEWEST_BACKUP_AT,
    count: 9,
    last_attempt_at: NEWEST_BACKUP_AT,
    last_error_kind: null,
    ...overrides,
  };
}

export const okBackup = backupStatus();

export const pendingBackup = backupStatus({
  state: 'pending',
  latest_at: null,
  count: 0,
  last_attempt_at: null,
});

export const disabledBackup = backupStatus({ state: 'disabled', last_attempt_at: null });

export const staleBackup = backupStatus({ state: 'stale', last_attempt_at: null });

/** Stale with no copy at all: a copy succeeded and its file was then removed. */
export const staleBackupWithNone = backupStatus({
  state: 'stale',
  latest_at: null,
  count: 0,
});

export const failedBackup = backupStatus({
  state: 'failed',
  last_attempt_at: FAILED_ATTEMPT_AT,
  last_error_kind: 'storage_error',
});

export const failedBackupWithNone = backupStatus({
  state: 'failed',
  latest_at: null,
  count: 0,
  last_attempt_at: FAILED_ATTEMPT_AT,
  last_error_kind: 'database_error',
});

/** Failed by a defect rather than a backup outcome: the backend serves no kind (spec 029). */
export const failedBackupWithNoKind = backupStatus({
  state: 'failed',
  last_attempt_at: FAILED_ATTEMPT_AT,
  last_error_kind: null,
});

/** R4: the directory cannot be listed, so the newest copy and the count are unknown. */
export const unreadableBackup = backupStatus({
  state: 'unreadable',
  latest_at: null,
  count: null,
  last_attempt_at: null,
  last_error_kind: null,
});

export function healthDetail(backup: BackupStatus = okBackup): HealthDetail {
  return { backup };
}

/** A handler that answers with `backup`, counting the requests it answered. */
export function serveBackup(backup: BackupStatus): {
  handler: HttpHandler;
  requests: () => number;
} {
  let requests = 0;
  const handler = http.get(HEALTH_DETAIL_PATH, () => {
    requests += 1;
    return HttpResponse.json(healthDetail(backup));
  });

  return { handler, requests: () => requests };
}
